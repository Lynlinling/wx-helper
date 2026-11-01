#include <cstdint>
#include <functional>
#include <intrin.h>

using DWORD = std::uint32_t;
using BOOL = int;
using HINSTANCE = void*;
using uintptr_t = std::uintptr_t;
#define WINAPI __stdcall
#define ERROR_SUCCESS 0
#define ERROR_INVALID_FUNCTION 1
#define ERROR_INVALID_HANDLE 6
#define ERROR_INVALID_PARAMETER 87
#define ERROR_BUSY 170
#define ERROR_NOT_READY 21
#define EXCEPTION_EXECUTE_HANDLER 1

struct SharedMessage { void* object; void* control; };
struct Location { const char* function; const char* file; std::uint32_t line; std::uint32_t pad; void* caller; };
static_assert(sizeof(void*) == 8);
static_assert(sizeof(SharedMessage) == 16);
static_assert(sizeof(Location) == 32);
static_assert(sizeof(std::function<void()>) == 64);

static volatile long g_started = 0;
static volatile long g_called = 0;
static volatile long g_active = 0;
static volatile long g_returned = 0;
static volatile long g_failed = 0;
static volatile long g_result = 0;

static void noop_callback() {
    _InterlockedIncrement(&g_active);
    _InterlockedExchange(&g_called, 1);
    _InterlockedDecrement(&g_active);
}

static std::function<void()> g_noop = []() { noop_callback(); };
static constexpr char kFunction[] = {'n','o','o','p',0};
static constexpr char kFile[] = {'s','c','h','e','d','u','l','e','_','p','r','o','b','e','_','4','2','.','c','p','p',0};

static DWORD guarded_get_scheduler(uintptr_t base, SharedMessage* output) {
    __try {
        using AppGetter = void* (__fastcall*)();
        using SchedulerGetter = SharedMessage* (__fastcall*)(void*, SharedMessage*);
        void* app = reinterpret_cast<AppGetter>(base + 0x45d10)();
        if (!app) return ERROR_NOT_READY;
        reinterpret_cast<SchedulerGetter>(base + 0x47310)(app, output);
        if (!output->object || !output->control) return ERROR_NOT_READY;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return 0xE0420001;
    }
    return ERROR_SUCCESS;
}

static DWORD guarded_schedule(uintptr_t base, void* scheduler, SharedMessage* future, Location* location, std::function<void()>* task) {
    __try {
        using Schedule = SharedMessage* (__fastcall*)(void*, SharedMessage*, Location*, std::function<void()>*, std::uint32_t);
        reinterpret_cast<Schedule>(base + 0x311990)(scheduler, future, location, task, 1);
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return 0xE0420002;
    }
    return ERROR_SUCCESS;
}

static DWORD guarded_release(SharedMessage* message) {
    if (!message || !message->control) return ERROR_SUCCESS;
    __try {
        auto* control = static_cast<unsigned char*>(message->control);
        auto* references = reinterpret_cast<volatile long*>(control + 8);
        if (_InterlockedDecrement(references) == 0) {
            void** vtable = *reinterpret_cast<void***>(control);
            if (!vtable || !vtable[0]) return ERROR_INVALID_FUNCTION;
            using Destroy = void (__fastcall*)(void*);
            reinterpret_cast<Destroy>(vtable[0])(control);
        }
        message->object = nullptr;
        message->control = nullptr;
    } __except (EXCEPTION_EXECUTE_HANDLER) {
        return 0xE0420003;
    }
    return ERROR_SUCCESS;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxScheduleNoop42(void* core_base) {
    if (!core_base) return ERROR_INVALID_PARAMETER;
    if (_InterlockedCompareExchange(&g_started, 1, 0) != 0) return ERROR_BUSY;
    SharedMessage scheduler{};
    SharedMessage future{};
    Location location{kFunction, kFile, 1, 0, nullptr};
    const uintptr_t base = reinterpret_cast<uintptr_t>(core_base);
    DWORD result = guarded_get_scheduler(base, &scheduler);
    if (result == ERROR_SUCCESS) {
        result = guarded_schedule(base, scheduler.object, &future, &location, &g_noop);
    }
    const DWORD future_release = guarded_release(&future);
    const DWORD scheduler_release = guarded_release(&scheduler);
    if (result == ERROR_SUCCESS && future_release != ERROR_SUCCESS) result = future_release;
    if (result == ERROR_SUCCESS && scheduler_release != ERROR_SUCCESS) result = scheduler_release;
    _InterlockedExchange(&g_result, static_cast<long>(result));
    _InterlockedExchange(&g_failed, result == ERROR_SUCCESS ? 0 : 1);
    _InterlockedExchange(&g_returned, 1);
    return result;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxScheduleNoopStatus42(void*) {
    DWORD status = 0;
    if (_InterlockedCompareExchange(&g_started, 0, 0)) status |= 0x01;
    if (_InterlockedCompareExchange(&g_called, 0, 0)) status |= 0x02;
    if (_InterlockedCompareExchange(&g_active, 0, 0)) status |= 0x04;
    if (_InterlockedCompareExchange(&g_returned, 0, 0)) status |= 0x08;
    if (_InterlockedCompareExchange(&g_failed, 0, 0)) status |= 0x10;
    return status;
}

extern "C" __declspec(dllexport) DWORD WINAPI WxScheduleNoopResult42(void*) {
    return static_cast<DWORD>(_InterlockedCompareExchange(&g_result, 0, 0));
}

BOOL WINAPI DllMain(HINSTANCE, DWORD, void*) { return 1; }
