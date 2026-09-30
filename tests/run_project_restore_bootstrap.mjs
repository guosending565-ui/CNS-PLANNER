// 沙箱内运行 node:test 文件的入口。
//
// 背景：DSH 文件沙箱下 `node --test <file>` 通过 child_process 以 pipe 捕获子进程输出，
// 会直接 EPERM（这是沙箱边界，不是测试失败）。改为在同进程内 `import` 该测试文件：
// `node:test` 在同一进程里照样会注册并执行测试并给出退出码。
import './project_restore_bootstrap_frontend.test.mjs';
