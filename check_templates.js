/* 离线校验：加载 frontend/app.js，编译全部组件模板，捕获模板编译错误 */
const fs = require("fs");
const path = require("path");
const Vue = require("/tmp/node_modules/vue/dist/vue.cjs.js");

// ---- 浏览器全局桩 ----
const store = {};
global.window = { addEventListener() {}, location: { hostname: "" } };
global.document = { getElementById: () => ({}) };
global.navigator = {};
global.fetch = async () => ({
  status: 200, ok: true,
  json: async () => ({}),
});
global.confirm = () => true;
global.setTimeout = (fn) => 0;

const src = fs.readFileSync(path.join(__dirname, "frontend/app.js"), "utf8");
// 去掉最后的挂载语句，避免依赖真实 DOM
const wrapped = src.replace(/createApp\(App\)\.mount\("#app"\);?/, "global.__APP = App;");
// 间接 eval 在当前作用域执行，const 声明通过挂载到 globalThis 的函数体内返回
// app.js 顶部会从全局 Vue 解构 API，这里仅注入全局 Vue
const fn = new Function("Vue",
  wrapped + "\n;return typeof App !== 'undefined' ? App : globalThis.__APP;");;
let App;
try {
  App = fn(Vue);
} catch (e) {
  console.error("加载 app.js 失败：", e.message);
  process.exit(1);
}

// 递归收集所有已注册组件（App.components 包括导入的组件对象）
const seen = new Set();
const errors = [];
function check(name, comp) {
  if (!comp || seen.has(comp)) return;
  seen.add(comp);
  const tpl = comp.template;
  if (typeof tpl === "string") {
    try {
      Vue.compile(tpl);
    } catch (e) {
      errors.push(`[${name}] ${e.message}`);
    }
  }
  const comps = comp.components || {};
  for (const [k, v] of Object.entries(comps)) check(k, v);
}
check("App", App);

if (errors.length) {
  console.log("模板编译错误：");
  errors.forEach((e) => console.log(" ✗ " + e));
  process.exit(1);
}
console.log(`全部 ${seen.size} 个组件模板编译通过`);
