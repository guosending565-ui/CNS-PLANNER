/**
 * 在**单进程内**运行 node:test 测试文件（DSH 受限沙箱下 `node --test` 必然会
 * spawn 子进程，而子进程管道被拒 → `spawn EPERM`）。
 *
 * 用法：node tools/run_node_tests.mjs tests/a.test.mjs tests/b.test.mjs
 *
 * 做法：动态 `import` 每个测试文件 —— node:test 的顶层 `test()` 在被导入时即注册
 * 并执行；失败信息由 node:test 自己打到 stdout/stderr，本脚本只负责在任一测试
 * 失败时把退出码置为非零。
 */
import test from 'node:test';
import {pathToFileURL} from 'node:url';
import {resolve} from 'node:path';

const files = process.argv.slice(2);
if (!files.length) {
  console.error('用法：node tools/run_node_tests.mjs <test-file> [...]');
  process.exit(2);
}

//: node:test 的默认 reporter 负责输出 ✔/✖、失败明细与汇总，并在任一测试失败时
//: 自行把退出码置为非零；这里只需要保证 import 阶段的错误也被计入退出码。
const importFailures = [];
for (const file of files) {
  try {
    await import(pathToFileURL(resolve(file)).href);
  } catch (exc) {
    importFailures.push(file);
    console.error(`[import 失败] ${file}\n${exc && exc.stack ? exc.stack : exc}`);
  }
}
if (importFailures.length) {
  console.error(`共 ${importFailures.length} 个测试文件无法加载`);
  process.exitCode = 1;
}
