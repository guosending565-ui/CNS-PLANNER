/**
 * F-03 前端回归：Step01 的「新建项目」入口与项目激活状态一致性。
 *
 * 独立运行：`node tests/f03_new_project_frontend.test.mjs`
 *
 * 锁定两件事：
 *  1. Step01 必须提供**新建项目**动作（项目名称 + 复用「项目数据存储位置」的目录），
 *     并如实说明新项目是空白的、不复制当前项目结果；
 *  2. 服务器 active project 身份优先于 draft 的"仅选中"状态 —— 项目已激活时不得再
 *     显示"已选择项目目录，尚未打开项目"（Round32-A M-01 的成因）。
 */
import assert from 'node:assert/strict';
import test from 'node:test';

import {PROJECT_OPEN_STATE_TEXT, projectOpenPanel, setProjectDirectoryDraft} from '../cns_planner/web/js/workflow/step01_project.js';

const ACTIVE_STEP = {identity: 'D:\\projects\\active', directory: 'D:\\projects\\active', facts: null};

test('active project 身份优先于 draft：不再显示"尚未打开项目"', () => {
  const typed = projectOpenPanel({getStep: () => ACTIVE_STEP, draft: 'D:\\projects\\typed-by-user'});
  assert.match(typed, new RegExp(PROJECT_OPEN_STATE_TEXT.active));
  assert.doesNotMatch(typed, /尚未打开项目/);
  assert.match(typed, /data-state="active"/);
  const same = projectOpenPanel({getStep: () => ACTIVE_STEP, draft: 'D:\\projects\\active'});
  assert.match(same, new RegExp(PROJECT_OPEN_STATE_TEXT.active));
  assert.doesNotMatch(same, /尚未打开项目/);
});

test('没有 active project 时，空状态与"已选择，尚未打开"都不变', () => {
  assert.match(projectOpenPanel({getStep: () => ({}), draft: ''}),
    new RegExp(PROJECT_OPEN_STATE_TEXT.empty));
  // 「选择…」只写 draft（模块级），面板渲染时读到的就是它 —— 这里用真实入口写 draft。
  setProjectDirectoryDraft('D:\\projects\\not-opened');
  const html = projectOpenPanel({getStep: () => ({}), draft: 'D:\\projects\\not-opened'});
  assert.match(html, new RegExp(PROJECT_OPEN_STATE_TEXT.selected));
  assert.doesNotMatch(html, new RegExp(PROJECT_OPEN_STATE_TEXT.active));
});

test('Step01 提供新建项目动作，且结论文案是"当前项目已激活"', () => {
  const html = projectOpenPanel({getStep: () => ({}), draft: ''});
  assert.match(html, /id="newProjectName"/);
  assert.match(html, /id="createProject"/);
  assert.match(html, /id="openProject"/);
  // 必须如实告知"空白 / 不复制当前项目"，不得让用户以为这是另存为。
  assert.match(html, /完全空白/);
  assert.match(html, /不复制当前项目/);
  assert.match(html, /公共默认配置/);
  assert.match(PROJECT_OPEN_STATE_TEXT.created, /当前项目已激活/);
  assert.match(PROJECT_OPEN_STATE_TEXT.creating, /新建项目/);
});
