# 原生文本适配

当前版本锁定 Windows x64 微信 4.1.15.13；使用前核对 `compatibility.json` 中的核心文件指纹。

运行需要的组件：

- `text_service.py`：文件传输助手、好友、群聊的原生文本服务。
- `filehelper_service.py`：接收通道和服务公共实现。
- `run_selftest.py`：进程、版本、远程调用与桥接生命周期封装。
- `build_text/text_bridge.dll`：当前文本收发和真实艾特桥接。
- `build/selftest_bridge.dll`：兼容文件传输助手后端。

源码 `text_bridge.cpp` 复用 `selftest_bridge.cpp` 的公共结构、任务调度和清理逻辑，因此两个源文件都要保留。`vendor/` 中只保留编译所需的 MinHook 源码、头文件和许可。

原始调试器、逆向数据库、跟踪日志、探针可执行文件、旧版本 DLL、编译中间产物和历史版本均不属于本目录。

GitHub 源码发布不附带预编译 DLL。请先进入 Visual Studio x64 Native Tools Command Prompt，再在项目目录运行 `build_bridges.ps1 -Target all`；这会在本机生成所需 DLL。账号和群聊从根目录的本地配置读取，没有预置真实用户。
