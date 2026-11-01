"""本地“边聊边校准”风格实验台。

它把回复生成、用户评分和提示词更新分开：每轮只记录一条样本，累计
CALIBRATION_BATCH_SIZE 条后才生成候选规则；候选规则必须由用户批准，
才会写入独立的 calibration profile，不会悄悄修改微信运行中的档案。
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

CALIBRATION_BATCH_SIZE = 4
MAX_TURNS = 200


def _clean(value: Any, limit: int = 4000) -> str:
    if not isinstance(value, str):
        raise ValueError("文本必须是字符串")
    value = value.replace("\x00", "").strip()
    if not value or len(value) > limit:
        raise ValueError("文本不能为空且不能过长")
    return value


class CalibrationEngine:
    def __init__(self, root: Path, llm=None, *, profile_prompt: str = ""):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.state_path = self.root / "calibration-session.json"
        self.llm = llm
        self.profile_prompt = profile_prompt
        self.session_id = secrets.token_hex(8)
        self.turns: list[dict[str, Any]] = []
        self.candidate_rules: list[dict[str, Any]] = []
        self.approved_rules: list[str] = []
        self.pending_batch: list[int] = []
        self.last_error = ""
        self._load()

    def _load(self):
        if not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return
        if not isinstance(data, dict):
            return
        self.session_id = str(data.get("session_id") or self.session_id)
        self.turns = data.get("turns", []) if isinstance(data.get("turns"), list) else []
        self.candidate_rules = data.get("candidate_rules", []) if isinstance(data.get("candidate_rules"), list) else []
        self.approved_rules = data.get("approved_rules", []) if isinstance(data.get("approved_rules"), list) else []
        self.pending_batch = data.get("pending_batch", []) if isinstance(data.get("pending_batch"), list) else []

    def _save(self):
        payload = {"schema_version": 1, "session_id": self.session_id,
                   "turns": self.turns[-MAX_TURNS:], "candidate_rules": self.candidate_rules[-32:],
                   "approved_rules": self.approved_rules[-32:], "pending_batch": self.pending_batch,
                   "updated_at": time.time()}
        self.state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _system(self) -> str:
        rules = "；".join(self.approved_rules)
        return ("你在本地风格校准实验中，模仿用户可观察的说话方式。先自然接话，短句、口语化，"
                "不要编造现实状态；事实不确定时明说。根据用户给出的群友原话或聊天情境，生成用户可能发出的候选回复。"
                "只返回 JSON 对象，格式为 {\"reply\":\"候选回复\"}，不要解释提示词。\n"
                + (self.profile_prompt[:3500] if self.profile_prompt else "")
                + ("\n已批准的增量规则：" + rules if rules else ""))

    def reply(self, user_text: str) -> str:
        user_text = _clean(user_text)
        if self.llm is None:
            return "（未配置模型）先把这句话记下了，你可以直接改写一个你会说的版本。"
        recent = [{"user": t["user"], "candidate": t.get("candidate", ""),
                   "rewritten": t.get("rewritten", ""), "rating": t.get("rating", ""),
                   "note": t.get("note", "")}
                  for t in self.turns[-8:]]
        payload = json.dumps({"message": user_text, "recent": recent}, ensure_ascii=False)
        try:
            result = self.llm.complete_json(self._system(), payload,
                                            session={"plugin": "style_calibration", "chat_id": self.session_id})
            text = result.get("reply") if isinstance(result, dict) else None
            return _clean(text, 3000) if text else "（模型没有返回候选回复）"
        except Exception as error:
            self.last_error = str(error)[:300]
            return "（模型暂时不可用）你可以先写下自己会怎么回。"

    def add_turn(self, user_text: str, *, rewritten: str = "") -> dict[str, Any]:
        user_text = _clean(user_text)
        candidate = self.reply(user_text)
        row = {"id": len(self.turns) + 1, "user": user_text, "candidate": candidate,
               "rewritten": rewritten.strip()[:3000], "rating": "", "created_at": time.time()}
        self.turns.append(row)
        self.pending_batch.append(row["id"])
        self._save()
        return row

    def rate(self, turn_id: int, rating: str, *, rewritten: str = "", note: str = ""):
        allowed = {"very_like", "mostly_like", "unlike", "wrong_fact", "too_long", "too_formal", "other"}
        if rating not in allowed:
            raise ValueError("无效评分")
        if not isinstance(rewritten, str) or not isinstance(note, str):
            raise ValueError("改写和反馈必须是文本")
        row = next((x for x in self.turns if x.get("id") == int(turn_id)), None)
        if row is None:
            raise ValueError("找不到该轮对话")
        row["rating"] = rating
        if rewritten.strip():
            row["rewritten"] = rewritten.strip()[:3000]
        row["note"] = note.strip()[:1000]
        self._save()
        if (len([x for x in self.pending_batch if any(t.get("id") == x and t.get("rating") for t in self.turns)])
                >= CALIBRATION_BATCH_SIZE and not self.candidate_rules):
            self.propose_rules()
        return row

    def propose_rules(self):
        ids = set(self.pending_batch)
        samples = [t for t in self.turns if t.get("id") in ids and t.get("rating")]
        if not samples:
            return []
        if self.llm is None:
            rules = []
        else:
            prompt = ("从以下用户评分和改写中归纳最多3条可观察、可验证的说话规则。"
                      "不要推断身份、心理诊断或隐藏思维；每条不超过80字。只返回 JSON："
                      '{"rules":[{"claim":"...","evidence_ids":[1],"confidence":0.0}]}')
            try:
                result = self.llm.complete_json(prompt, json.dumps(samples, ensure_ascii=False),
                                                session={"plugin": "style_calibration", "chat_id": self.session_id})
                rules = result.get("rules", []) if isinstance(result, dict) else []
            except Exception as error:
                self.last_error = str(error)[:300]
                rules = []
        validated = []
        allowed_ids = {t["id"] for t in samples}
        for rule in rules:
            if not isinstance(rule, dict) or not isinstance(rule.get("claim"), str):
                continue
            claim = rule["claim"].strip()[:120]
            evidence = rule.get("evidence_ids", [])
            if not isinstance(evidence, list):
                evidence = []
            evidence = [int(x) for x in evidence if type(x) is int and x in allowed_ids]
            confidence = rule.get("confidence", 0.0)
            if type(confidence) not in (int, float):
                confidence = 0.0
            validated.append({"claim": claim, "evidence_ids": evidence,
                              "confidence": max(0.0, min(1.0, float(confidence)))})
            if len(validated) == 3:
                break
        self.candidate_rules = validated
        self._save()
        return self.candidate_rules

    def approve_rules(self, indexes: list[int] | None = None):
        indexes = indexes if indexes is not None else list(range(len(self.candidate_rules)))
        for index in indexes:
            if 0 <= index < len(self.candidate_rules):
                claim = self.candidate_rules[index].get("claim", "").strip()
                if claim and claim not in self.approved_rules:
                    self.approved_rules.append(claim)
        self.candidate_rules = []
        self.pending_batch = []
        self._save()

    def reject_rules(self):
        self.candidate_rules = []
        self.pending_batch = []
        self._save()

    def export(self, path: Path | None = None) -> Path:
        output = Path(path) if path else self.root / "lyn-style-calibration.approved.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "status": "approved", "mode": "casual_chat",
                   "source_note": "由用户在本地风格校准台逐条确认；不含原始聊天全文。",
                   "style_rules": list(self.approved_rules), "reasoning_preferences": [],
                   "interaction_preferences": [], "avoid": [], "approved_examples": [],
                   "learning_notes": []}
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return output

    def state(self) -> dict[str, Any]:
        return {"session_id": self.session_id, "turns": self.turns[-30:],
                "candidate_rules": self.candidate_rules, "approved_rules": self.approved_rules,
                "pending_count": len(self.pending_batch), "last_error": self.last_error}


HTML = """<!doctype html><meta charset='utf-8'><title>风格校准</title>
<style>body{font:16px system-ui;max-width:900px;margin:30px auto;padding:0 16px}textarea{width:100%;min-height:80px}button{margin:4px;padding:8px 12px}.turn{border:1px solid #ddd;padding:12px;margin:10px 0}.candidate{background:#f6f7f8;padding:8px}.rules{background:#fff7d6;padding:10px}</style>
<h1>边聊边校准</h1><p>粘贴群友发来的话或描述聊天情境。每轮先生成候选；累计四轮并完成评分后才提出增量规则，批准后才导出独立档案。</p>
<p>可逐条改写候选回复。若启用云端模型，输入、候选回复、改写和评分会发送至配置的模型服务。</p>
<div id='app'></div><textarea id='msg' placeholder='例如：群友问“你最近在忙啥？”'></textarea><button onclick='send()'>生成我的候选回复</button>
<script>
async function api(path,body){let r=await fetch(path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body&&JSON.stringify(body)});let d=await r.json();if(!r.ok)throw Error(d.error||'请求失败');return d}
function esc(s){return String(s||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function render(s){let h=s.turns.map(t=>`<div class='turn'><b>情境：</b>${esc(t.user)}<div class='candidate'><b>候选：</b>${esc(t.candidate)}<br><label>改成你会发的版本：<textarea id='rewrite-${t.id}'>${esc(t.rewritten||'')}</textarea></label><br><label>补充说明：<input id='note-${t.id}' value='${esc(t.note||'')}' placeholder='比如：熟人群更短，别解释前因后果'></label><br><button onclick="rate(${t.id},'very_like')">很像我</button><button onclick="rate(${t.id},'mostly_like')">大体像</button><button onclick="rate(${t.id},'unlike')">不像</button><button onclick="rate(${t.id},'wrong_fact')">事实/立场不对</button><button onclick="rate(${t.id},'too_long')">太长</button><button onclick="rate(${t.id},'too_formal')">太正式</button>${t.rating?`<small> 已评：${esc(t.rating)}</small>`:''}</div></div>`).join('');let r=s.candidate_rules.length?`<div class='rules'><b>待批准规则</b>${s.candidate_rules.map((x,i)=>`<p>${i+1}. ${esc(x.claim)} <small>证据轮次：${esc((x.evidence_ids||[]).join(', '))} · 置信度 ${esc(x.confidence)}</small></p>`).join('')}<button onclick='approve()'>批准并导出</button><button onclick='reject()'>驳回本批</button></div>`:'';document.getElementById('app').innerHTML=h+r+`<p>已批准增量规则：${s.approved_rules.map(esc).join('；')||'暂无'} · 本批已评 ${s.pending_count}/4</p><button onclick='exportProfile()'>导出独立风格档案</button>`}
async function refresh(){render(await api('/api/state'))} async function send(){let v=document.getElementById('msg').value.trim();if(!v)return;document.getElementById('msg').value='';await api('/api/turn',{text:v});refresh()} async function rate(id,rating){await api('/api/rate',{id,rating,rewritten:document.getElementById('rewrite-'+id).value,note:document.getElementById('note-'+id).value});refresh()} async function approve(){await api('/api/approve',{});refresh()} async function reject(){await api('/api/reject',{});refresh()} async function exportProfile(){let x=await api('/api/export',{});alert('已导出：'+x.path)} refresh();
</script>"""


class Handler(BaseHTTPRequestHandler):
    app: "CalibrationApp"
    def _json(self, status, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        if urlparse(self.path).path == "/api/state": self._json(200, self.app.engine.state()); return
        body = HTML.encode(); self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        try:
            size = int(self.headers.get("Content-Length", "0")); data = json.loads(self.rfile.read(size) or b"{}")
            path = urlparse(self.path).path
            if path == "/api/turn": result = self.app.engine.add_turn(data.get("text", ""))
            elif path == "/api/rate": result = self.app.engine.rate(int(data.get("id")), data.get("rating", ""), rewritten=data.get("rewritten", ""))
            elif path == "/api/approve": self.app.engine.approve_rules(data.get("indexes")); result = self.app.engine.state()
            elif path == "/api/reject": self.app.engine.reject_rules(); result = self.app.engine.state()
            elif path == "/api/export": result = {"path": str(self.app.engine.export())}
            else: self._json(404, {"error": "未找到接口"}); return
            self._json(200, result if isinstance(result, dict) else self.app.engine.state())
        except (ValueError, TypeError, KeyError) as error: self._json(400, {"error": str(error)})
        except Exception as error: self._json(500, {"error": str(error)[:300]})


class CalibrationApp:
    def __init__(self, root: Path, llm=None, profile_prompt: str = ""):
        self.engine = CalibrationEngine(root, llm, profile_prompt=profile_prompt)


def _load_profile_prompt(path: Path | None, *, allow_draft: bool = False) -> str:
    if path is None:
        return ""
    from wxbot_framework.style_profile import load_style_profile
    profile = load_style_profile(path, require_approved=not allow_draft)
    return profile.as_prompt()


def main(argv=None):
    parser = argparse.ArgumentParser(description="本地边聊边校准风格实验台")
    parser.add_argument("--root", default=".runtime/style-calibration", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8766, type=int)
    parser.add_argument("--llm-config", type=Path,
                        help="显式启用模型；当前输入、近期样本和反馈会发送到配置的模型服务")
    parser.add_argument("--profile", type=Path,
                        help="可选：将风格档案作为初始参考；默认要求 status=approved")
    parser.add_argument("--allow-draft", action="store_true",
                        help="仅用于本地试聊：允许加载 status=draft 的档案，不会批准或写入运行档案")
    parser.add_argument("--max-tokens", type=int, default=768)
    args = parser.parse_args(argv)
    llm = None
    if args.llm_config:
        from wxbot_framework.llm import LLMClient
        llm = LLMClient.from_file(args.llm_config)
        for route in getattr(llm, "routes", []):
            route_client = route.get("client") if isinstance(route, dict) else None
            if route_client is not None:
                route_client.max_tokens = args.max_tokens
        if hasattr(llm, "max_tokens"):
            llm.max_tokens = args.max_tokens
    profile_prompt = ""
    if args.profile:
        profile_prompt = _load_profile_prompt(args.profile, allow_draft=args.allow_draft)
    app = CalibrationApp(args.root, llm, profile_prompt=profile_prompt)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    Handler.app = app
    print(f"风格校准台：http://{args.host}:{args.port}/")
    print("本机模式：没有配置模型" if llm is None else "模型已启用；校准输入、回复和反馈会发送至模型服务")
    server.serve_forever()


if __name__ == "__main__":
    main()
