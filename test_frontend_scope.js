/* 前端数据隔离测试：
 * 1) A 牛舍账号看到的健康/挤奶记录，退出后登录 B 牛舍账号不得残留；
 * 2) A 账号的在途慢响应晚于 B 登录返回时，不得覆盖 B 的数据（代际令牌）。
 */
const fs = require("fs");
const path = require("path");

let S;            // app.js 的全局 reactive 状态
let doLogin, doLogout;
let routes = {};  // path -> 返回数组（按调用次序）或函数
let pendingResolve = {};
globalThis.__release = (p, b) => {
  const f = pendingResolve[p];
  pendingResolve[p] = null;
  f && f(b);
};
let callCount = {};

global.window = { addEventListener() {}, location: { hostname: "" } };
global.document = {
  getElementById: () => ({}),
  createElement: () => ({ setAttribute() {}, appendChild() {} }),
};
global.navigator = {};
global.confirm = () => true;

// 可控 fetch：每个路由可配置返回队列，或挂起等待 trigger
global.fetch = async (url, opts = {}) => {
  const p = url.split("?")[0];
  callCount[p] = (callCount[p] || 0) + 1;
  let handler = routes[p];
  if (typeof handler === "function") handler = handler(opts, url);
  const next = Array.isArray(handler) ? handler[Math.min(callCount[p] - 1, handler.length - 1)] : handler;
  if (next && next.__hang) {
    await new Promise((res) => { pendingResolve[p] = res; });
  }
  return makeResp(next, next && next.status ? next.status : 200);
};
function makeResp(body, status) {
  return { status, ok: status >= 200 && status < 300, json: async () => body };
}

const src = fs.readFileSync(path.join(__dirname, "frontend/app.js"), "utf8")
  .replace(/createApp\(App\)\.mount\("#app"\);?/, "")
  // 暴露内部绑定便于断言
  .replace("let sessionGen = 0;",
    'let sessionGen = 0;\nglobalThis.__t = { get S(){return S;}, doLogin, doLogout, setRoutes(v){routes=v;} };');

// app.js 仅使用 Vue 的响应式 API（模板编译在本测试中不执行），
// 用不依赖 DOM 的 @vue/reactivity 构造一个等价 Vue 全局
const reactivity = require("/tmp/node_modules/@vue/reactivity/dist/reactivity.cjs.js");
const noop = () => {};
const Vue = {
  reactive: reactivity.reactive,
  ref: reactivity.ref,
  computed: reactivity.computed,
  watch: noop,
  onMounted: noop,
  nextTick: (fn) => Promise.resolve().then(fn),
  createApp: () => ({ mount: noop, component: noop }),
};
new Function("Vue", src)(Vue);
const t = globalThis.__t;

function cow(id, tag, shed) { return { id, ear_tag: tag, shed_id: shed }; }
const me = (roles, sheds) => ({
  id: 1, username: "u", display_name: "测试", roles, role_labels: roles,
  permissions: [], is_admin: false, global_scope: false,
  sheds: sheds.map((id) => ({ id, code: id + "栋", name: "" })),
});
const emptyPage = () => [];

(async () => {
  let fails = 0;
  const ok = (n, c, d) => { console.log((c ? "  ✓ " : "  ✗ ") + n + (c ? "" : "  " + JSON.stringify(d))); if (!c) fails++; };

  // ---- 场景 1：A栋兽医 -> 退出 -> B栋兽医 ----
  t.setRoutes({
    "/api/auth/login": {
      // 第一次登录 A(负责牛舍1)，第二次登录 B(负责牛舍2)
      __seq: 0,
    },
  });
  // 用函数形式按登录用户名返回不同身份
  routes = {
    "/api/auth/login": (opts) => {
      const b = JSON.parse(opts.body);
      return b.username === "vetA"
        ? me(["vet"], [1])
        : me(["vet"], [2]);
    },
    "/api/auth/me": me(["vet"], [1]),
    "/api/auth/logout": { ok: true },
    "/api/sheds": [{ id: 1, code: "1栋" }, { id: 2, code: "2栋" }],
    "/api/cows": [
      [cow(10, "A1", 1), cow(11, "A2", 1)],
      [cow(20, "B1", 2)],
    ],
    "/api/health": [
      [{ id: 100, cow_id: 10, cow_ear_tag: "A1" }],
      [{ id: 200, cow_id: 20, cow_ear_tag: "B1" }],
    ],
    "/api/medications": [[{ id: 1, cow_id: 10 }], [{ id: 2, cow_id: 20 }]],
    "/api/estruses": [[{ id: 1, cow_id: 10 }], [{ id: 2, cow_id: 20 }]],
    "/api/milkings": [[{ id: 1, cow_id: 10, created_by: 1 }], [{ id: 2, cow_id: 20, created_by: 1 }]],
    "/api/drugs": [[]],
    "/api/dashboard": [{ today: "2026-09-16" }],
    "/api/reminders": [[]],
    "/api/anomalies": [[]],
  };

  await t.doLogin("vetA", "x");
  S = t.S;
  ok("A账号登录后看到A栋牛只", S.cows.map((c) => c.ear_tag).join() === "A1,A2", S.cows.map((c) => c.ear_tag));
  S.health = await (await fetch("/api/health")).json();
  S.meds = await (await fetch("/api/medications")).json();
  S.estruses = await (await fetch("/api/estruses")).json();
  ok("A账号看过健康/用药/发情记录", S.health.length === 1 && S.meds.length === 1 && S.estruses.length === 1);

  await t.doLogout();
  ok("退出后健康记录被清空", S.health.length === 0, S.health);
  ok("退出后用药/发情/挤奶/牛只全部清空",
     S.meds.length === 0 && S.estruses.length === 0 && S.milkings.length === 0 && S.cows.length === 0);
  ok("退出后用户身份为空", S.user === null && S.authed === false);

  await t.doLogin("vetB", "x");
  ok("B账号只看到B栋牛只", S.cows.length === 1 && S.cows[0].ear_tag === "B1", S.cows);
  // 进入健康页会重新拉取（切到第二批数据）
  ok("B账号基础数据中无A账号健康记录残留", S.health.length === 0, S.health);
  const h = await (await fetch("/api/health")).json();
  ok("重新拉取得到的是B栋记录(A1不应出现)", h.length === 1 && h[0].cow_ear_tag === "B1", h);

  // ---- 场景 2：A 的慢响应在 B 登录后才返回，不得覆盖 B ----
  await t.doLogout();
  callCount = {};
  routes = {
    "/api/auth/login": (opts) => {
      const b = JSON.parse(opts.body);
      return b.username === "vetA" ? me(["vet"], [1]) : me(["vet"], [2]);
    },
    "/api/auth/logout": { ok: true },
    "/api/sheds": [],
    "/api/dashboard": null,
    "/api/reminders": [],
    "/api/anomalies": [],
    "/api/drugs": [],
    // cows 第一次调用挂起（A 登录的慢响应），第二次调用（B 登录）立即返回 B 牛只
    "/api/cows": [
      { __hang: true, body: [cow(10, "A1", 1)] },
      [cow(20, "B1", 2)],
    ],
  };
  // 触发 A 登录（其 cows 请求挂起）
  const loginA = t.doLogin("vetA", "x");
  await new Promise((r) => setTimeout(r, 20));
  // A 尚未拿到 cows（慢响应挂起）——直接退出并登录 B
  await t.doLogout();
  const loginB = t.doLogin("vetB", "x");
  await new Promise((r) => setTimeout(r, 20));
  ok("B 登录后立即看到 B 牛只", S.cows.length === 1 && S.cows[0].ear_tag === "B1", S.cows);
  // A 的挂起响应才返回
  globalThis.__release("/api/cows");
  await Promise.all([loginA.catch(() => {}), loginB]);
  await new Promise((r) => setTimeout(r, 20));
  ok("A 的晚到响应未覆盖 B 的牛只数据", S.cows.length === 1 && S.cows[0].ear_tag === "B1", S.cows);

  console.log(fails ? `\n失败 ${fails} 项` : "\n全部通过");
  process.exit(fails ? 1 : 0);
})();
