# 验证证据

该目录保存与 commit、环境和测试配置绑定的运行证据。禁止只提交截图而不保留命令、配置和原始输出。

`integration-test-2026-07-10.txt` 是历史记录，其中引用的 commit 不在当前仓库历史中，不能作为
当前 HEAD 的验证证据。当前提交应以带有效 commit SHA 的最新验证文件、CI 和新一轮 Compose
原始输出为准。

每次集成测试至少记录：

- 日期和 commit SHA；
- 操作系统、Python/Docker 版本；
- 非敏感环境变量；
- 单元测试、smoke test 输出；
- Recorder summary；
- 失败项和后续修复链接。
