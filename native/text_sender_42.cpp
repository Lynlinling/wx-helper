#include "schedule_probe_42.cpp"
#include <cstdlib>
#include <cstring>
#include <memory>
#include <new>
using LONG = long;
using HANDLE = void*;
using HMODULE = void*;
using UINT = std::uint32_t;
extern "C" __declspec(dllimport) DWORD WINAPI GetCurrentProcessId();
extern "C" __declspec(dllimport) HMODULE WINAPI GetModuleHandleW(const wchar_t*);
extern "C" __declspec(dllimport) int WINAPI MultiByteToWideChar(UINT, DWORD, const char*, int, wchar_t*, int);
static constexpr DWORD ERROR_NOT_ENOUGH_MEMORY = 8;
static constexpr DWORD ERROR_GEN_FAILURE = 31;
static constexpr DWORD ERROR_MOD_NOT_FOUND = 126;
struct SenderConfig { DWORD size; DWORD pid; char target[128]; char allowed_group[128]; char text[1024]; };
struct SmallString { char data[16]; std::uint64_t length; std::uint64_t capacity; };
struct TextRequest { SmallString receiver; SmallString body; };
static_assert(sizeof(SenderConfig) == 1288);
static_assert(sizeof(SmallString) == 32);
static_assert(sizeof(TextRequest) == 64);
static volatile LONG s_busy = 0, s_queued = 0, s_task_started = 0, s_builder_returned = 0;
static volatile LONG s_completed = 0, s_success = 0, s_failed = 0, s_error = 0;
static volatile LONG s_entry_returned = 0, s_completion_destroyed = 0, s_active_tasks = 0, s_live_completions = 0;
static uintptr_t s_core_base = 0;
static bool valid_id(const char* value, size_t capacity) { size_t n = strnlen_s(value, capacity); if (!n || n >= capacity) return false; for (size_t i = 0; i < n; ++i) { unsigned char c = value[i]; if (!((c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || c == '_' || c == '.' || c == '@' || c == '-')) return false; } return true; }
static bool valid_target(const SenderConfig& c) {
    if (!valid_id(c.target, sizeof(c.target))) return false;
    if (std::strcmp(c.target, "filehelper") == 0) return true;
    if (!valid_id(c.allowed_group, sizeof(c.allowed_group))) return false;
    size_t n = std::strlen(c.allowed_group);
    if (n > 9 && std::strcmp(c.allowed_group + n - 9, "@chatroom") == 0 &&
        std::strcmp(c.target, c.allowed_group) == 0) return true;
    return false;
}
static bool valid_text(const char* text) { size_t n = strnlen_s(text, 1024); return n > 0 && n < 1024 && MultiByteToWideChar(65001, 8, text, static_cast<int>(n), nullptr, 0) > 0; }
static bool set_native_string(SmallString* s, const char* text) { size_t n = std::strlen(text); if (n <= 15) { std::memcpy(s->data, text, n + 1); s->length = n; return true; } if (n > 1023 || !s_core_base) return false; using Allocate = void* (__fastcall*)(size_t); char* p = static_cast<char*>(reinterpret_cast<Allocate>(s_core_base + 0x76a53bc)(n + 1)); if (!p) return false; std::memcpy(p, text, n + 1); std::memcpy(s->data, &p, sizeof(p)); s->length = s->capacity = n; return true; }
static void free_native_string(SmallString* s) { if (s->capacity > 15 && s_core_base) { void* p = *reinterpret_cast<void**>(s->data); using Free = void (__fastcall*)(void*, size_t); reinterpret_cast<Free>(s_core_base + 0x76a5400)(p, s->capacity + 1); } *s = {}; s->capacity = 15; }
struct SenderCompletion { volatile LONG references; std::uint32_t padding; void (__fastcall* invoke)(SenderCompletion*, const unsigned char*); void (__fastcall* destroy)(SenderCompletion*); SenderConfig config; };
static_assert(sizeof(SenderCompletion) == 1312);
static void maybe_release_busy() { if (_InterlockedCompareExchange(&s_entry_returned, 0, 0) && !_InterlockedCompareExchange(&s_active_tasks, 0, 0) && !_InterlockedCompareExchange(&s_live_completions, 0, 0) && (_InterlockedCompareExchange(&s_completion_destroyed, 0, 0) || _InterlockedCompareExchange(&s_failed, 0, 0))) _InterlockedExchange(&s_busy, 0); }
static void __fastcall completion_invoke(SenderCompletion*, const unsigned char* result) { bool ok = result && result[0] == 1; _InterlockedExchange(&s_success, ok ? 1 : 0); _InterlockedExchange(&s_error, ok ? 0 : ERROR_GEN_FAILURE); _InterlockedExchange(&s_completed, 1); if (!ok) _InterlockedExchange(&s_failed, 1); }
static void __fastcall completion_destroy(SenderCompletion* self) { if (!_InterlockedCompareExchange(&s_completed, 0, 0)) { _InterlockedExchange(&s_error, ERROR_GEN_FAILURE); _InterlockedExchange(&s_failed, 1); } delete self; if (_InterlockedDecrement(&s_live_completions) == 0) _InterlockedExchange(&s_completion_destroyed, 1); maybe_release_busy(); }
static DWORD send_text_on_task(const SenderConfig* config) { if (!config || _InterlockedCompareExchange(&s_live_completions, 0, 0)) return ERROR_BUSY; TextRequest request{}; request.receiver.capacity = request.body.capacity = 15; SmallString description{}; description.capacity = 15; if (!set_native_string(&request.receiver, config->target) || !set_native_string(&request.body, config->text) || !set_native_string(&description, "wxbot.text")) { free_native_string(&request.receiver); free_native_string(&request.body); return ERROR_INVALID_PARAMETER; } SenderCompletion* completion = new (std::nothrow) SenderCompletion{1, 0, completion_invoke, completion_destroy, *config}; if (!completion) { free_native_string(&request.receiver); free_native_string(&request.body); return ERROR_NOT_ENOUGH_MEMORY; } _InterlockedIncrement(&s_live_completions); DWORD status = ERROR_SUCCESS; __try { using Builder = void (__fastcall*)(void*, TextRequest*, SmallString*, SenderCompletion**); reinterpret_cast<Builder>(s_core_base + 0x5721040)(nullptr, &request, &description, &completion); } __except (EXCEPTION_EXECUTE_HANDLER) { status = ERROR_INVALID_FUNCTION; _InterlockedExchange(&s_error, status); _InterlockedExchange(&s_failed, 1); } _InterlockedExchange(&s_builder_returned, 1); if (completion) completion_destroy(completion); free_native_string(&request.receiver); free_native_string(&request.body); return status; }
struct SenderTaskLease { SenderConfig config; explicit SenderTaskLease(const SenderConfig& c) : config(c) {} };
static void run_send_task(const SenderConfig* config) { _InterlockedIncrement(&s_task_started); DWORD result = send_text_on_task(config); _InterlockedExchange(&s_builder_returned, 1); if (result != ERROR_SUCCESS) { _InterlockedExchange(&s_error, result); _InterlockedExchange(&s_failed, 1); if (!_InterlockedCompareExchange(&s_live_completions, 0, 0)) _InterlockedExchange(&s_completion_destroyed, 1); } }
static DWORD schedule_send_task(const SenderConfig& config) { try { auto lease = std::make_shared<SenderTaskLease>(config); std::function<void()> task = [lease]() { _InterlockedIncrement(&s_active_tasks); run_send_task(&lease->config); _InterlockedDecrement(&s_active_tasks); maybe_release_busy(); }; Location location{"wxbot.sender42", "text_sender_42.cpp", 1, 0, nullptr}; SharedMessage scheduler{}, future{}; DWORD status = guarded_get_scheduler(s_core_base, &scheduler); if (status == ERROR_SUCCESS) status = guarded_schedule(s_core_base, scheduler.object, &future, &location, &task); DWORD future_status = guarded_release(&future); DWORD scheduler_status = guarded_release(&scheduler); if (status == ERROR_SUCCESS && future_status != ERROR_SUCCESS) status = future_status; if (status == ERROR_SUCCESS && scheduler_status != ERROR_SUCCESS) status = scheduler_status; return status; } catch (...) { return ERROR_NOT_ENOUGH_MEMORY; } }
extern "C" __declspec(dllexport) DWORD WINAPI WxSendText42(void* parameter) { if (!parameter) return ERROR_INVALID_PARAMETER; if (_InterlockedCompareExchange(&s_busy, 1, 0) != 0) return ERROR_BUSY; if (_InterlockedCompareExchange(&s_active_tasks, 0, 0) || _InterlockedCompareExchange(&s_live_completions, 0, 0)) { _InterlockedExchange(&s_busy, 0); return ERROR_BUSY; } _InterlockedExchange(&s_queued, 0); _InterlockedExchange(&s_task_started, 0); _InterlockedExchange(&s_builder_returned, 0); _InterlockedExchange(&s_completed, 0); _InterlockedExchange(&s_success, 0); _InterlockedExchange(&s_failed, 0); _InterlockedExchange(&s_error, 0); _InterlockedExchange(&s_entry_returned, 0); _InterlockedExchange(&s_completion_destroyed, 0); const SenderConfig config = *static_cast<const SenderConfig*>(parameter); if (config.size != sizeof(SenderConfig) || config.pid != GetCurrentProcessId() || !valid_target(config) || !valid_text(config.text)) { _InterlockedExchange(&s_error, ERROR_INVALID_PARAMETER); _InterlockedExchange(&s_failed, 1); _InterlockedExchange(&s_completion_destroyed, 1); _InterlockedExchange(&s_entry_returned, 1); maybe_release_busy(); return ERROR_INVALID_PARAMETER; } HMODULE core = GetModuleHandleW(L"Weixin.dll"); if (!core) { _InterlockedExchange(&s_error, ERROR_MOD_NOT_FOUND); _InterlockedExchange(&s_failed, 1); _InterlockedExchange(&s_completion_destroyed, 1); _InterlockedExchange(&s_entry_returned, 1); maybe_release_busy(); return ERROR_MOD_NOT_FOUND; } s_core_base = reinterpret_cast<uintptr_t>(core); DWORD status = schedule_send_task(config); if (status == ERROR_SUCCESS) _InterlockedExchange(&s_queued, 1); else { _InterlockedExchange(&s_error, status); _InterlockedExchange(&s_failed, 1); _InterlockedExchange(&s_completion_destroyed, 1); } _InterlockedExchange(&s_entry_returned, 1); maybe_release_busy(); return status; }
extern "C" __declspec(dllexport) DWORD WINAPI WxSenderStatus42(void*) { DWORD v = 0; if (_InterlockedCompareExchange(&s_busy, 0, 0)) v |= 1; if (_InterlockedCompareExchange(&s_queued, 0, 0)) v |= 2; if (_InterlockedCompareExchange(&s_task_started, 0, 0)) v |= 4; if (_InterlockedCompareExchange(&s_builder_returned, 0, 0)) v |= 8; if (_InterlockedCompareExchange(&s_completed, 0, 0)) v |= 16; if (_InterlockedCompareExchange(&s_success, 0, 0)) v |= 32; if (_InterlockedCompareExchange(&s_failed, 0, 0)) v |= 64; if (_InterlockedCompareExchange(&s_entry_returned, 0, 0)) v |= 128; return v; }
extern "C" __declspec(dllexport) DWORD WINAPI WxSenderResult42(void*) { return static_cast<DWORD>(_InterlockedCompareExchange(&s_error, 0, 0)); }
