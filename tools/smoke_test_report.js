/**
 * 报告渲染冒烟测试：用最小 DOM 桩在 Node 里执行 report.py 产出的 JS，
 * 确保 renderAll() 及所有图表渲染函数不抛异常。
 *
 * 用法：node tools/smoke_test_report.js <报告html路径>
 */
const fs = require('fs');
const path = require('path');

const file = process.argv[2];
if (!file) { console.error('用法: node tools/smoke_test_report.js <报告.html>'); process.exit(2); }

const html = fs.readFileSync(file, 'utf8');
const scripts = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (scripts.length < 2) { console.error('未找到内嵌脚本'); process.exit(2); }

/* ---------------- 最小 DOM 桩 ---------------- */
let created = 0;
class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.style = {};
    this._html = '';
    this.className = '';
    this.title = '';
    this.disabled = false;
    this.clientWidth = 560;
    created++;
  }
  set innerHTML(v) { this._html = String(v); if (v === '') this.children = []; }
  get innerHTML() { return this._html; }
  appendChild(c) { this.children.push(c); return c; }
  removeChild(c) { this.children = this.children.filter(x => x !== c); return c; }
  setAttribute() {}
  getAttribute() { return 'dark'; }
  addEventListener() {}
  click() {}
}

const registry = {};
function makeEl(tag) {
  const e = new El(tag);
  return e;
}
const ids = ['kpis', 'p-summary', 'p-radar', 'p-tags', 'p-actors', 'p-studios',
  'c-tags', 'c-cloud', 'c-tech', 'c-actors', 't-actors', 't-copair', 'c-acount',
  'c-studios', 'c-series', 'c-directors', 'c-prefixes', 'c-res', 'c-censor',
  'c-censor-tip', 'c-year', 'c-month', 'c-hour', 'c-week', 'c-rating', 'c-runtime',
  'c-heat', 't-pairs', 'c-kwcloud', 'c-kwbar', 't-movies', 'app'];
ids.forEach(id => { registry['#' + id] = makeEl('div'); });

global.document = {
  documentElement: { setAttribute() {}, getAttribute: () => 'dark' },
  createElement: makeEl,
  querySelector: sel => registry[sel] || makeEl('div'),
  getElementById: id => registry['#' + id] || makeEl('div'),
  addEventListener() {},
  body: { appendChild() {}, removeChild() {} },
};
global.window = {
  addEventListener() {},
  __DATA__: null,
};
global.localStorage = { getItem: () => null, setItem() {} };
global.Blob = class { constructor() {} };
global.URL = { createObjectURL: () => 'blob:x', revokeObjectURL() {} };

/* ---------------- 执行 ---------------- */
let failed = 0;
function check(name, fn) {
  try { fn(); console.log('  PASS  ' + name); }
  catch (e) { failed++; console.log('  FAIL  ' + name + ' -> ' + e.message); }
}

try {
  // 数据脚本
  new Function('window', scripts[0])(global.window);
  console.log('数据注入: ' + (global.window.__DATA__ ? 'OK' : 'FAIL'));
  if (!global.window.__DATA__) { process.exit(1); }
  const D = global.window.__DATA__;
  console.log('作品数: ' + (D.overview && D.overview.total));
  console.log('内嵌明细: ' + ((D.movies || []).length));

  // 渲染脚本
  new Function('window', 'document', 'localStorage', 'Blob', 'URL', 'setTimeout',
    'clearTimeout', scripts[1])(
    global.window, global.document, global.localStorage, global.Blob, global.URL,
    (f) => f, () => {});

  console.log('\n渲染检查:');
  check('KPI 卡片已渲染', () => {
    const h = registry['#kpis'].innerHTML;
    if (!h || h.indexOf('kpi') < 0) throw new Error('kpis 为空');
  });
  check('画像总结已渲染', () => {
    if (!registry['#p-summary'].textContent) throw new Error('summary 为空');
  });
  check('雷达图 SVG 已生成', () => {
    const svg = JSON.stringify(registry['#p-radar'].innerHTML || '');
    if (registry['#p-radar'].children.length === 0) throw new Error('未生成节点');
  });
  ['c-tags', 'c-cloud', 'c-actors', 't-actors', 'c-studios', 'c-series',
   'c-directors', 'c-prefixes', 'c-res', 'c-censor', 'c-year', 'c-month',
   'c-hour', 'c-week', 'c-rating', 'c-runtime', 'c-heat', 't-pairs',
   'c-kwcloud', 'c-kwbar', 't-movies', 't-copair', 'c-acount', 'c-tech',
   'p-tags', 'p-actors', 'p-studios', 'c-censor-tip'].forEach(id => {
    check('区块 ' + id, () => {
      const node = registry['#' + id];
      const has = node.children.length > 0 || String(node.innerHTML || '').length > 0;
      if (!has) throw new Error(id + ' 无内容');
    });
  });

  console.log('\n创建的 DOM 节点数: ' + created);
} catch (e) {
  console.error('\n执行异常: ' + e.message);
  console.error(e.stack.split('\n').slice(0, 6).join('\n'));
  process.exit(1);
}

console.log(failed ? `\n结果: ${failed} 项失败` : '\n结果: 全部通过');
process.exit(failed ? 1 : 0);
