/* 智慧牧场管理系统前端（Vue 3, 无构建） */
const { createApp, reactive, ref, computed, onMounted, watch, nextTick } = Vue;

/* ---------------- 工具 ---------------- */
const API = "";
async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (res.status === 204) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const msg = typeof data.detail === "string" ? data.detail : "请求失败（" + res.status + "）";
    throw new Error(msg);
  }
  return data;
}
const todayStr = () => new Date().toISOString().slice(0, 10);
const addDays = (s, n) => {
  const d = new Date(s + "T00:00:00");
  d.setDate(d.getDate() + n);
  return d.toISOString().slice(0, 10);
};
const fmtSCC = (v) => (v == null ? "-" : v >= 10000 ? (v / 10000).toFixed(1) + " 万" : v);

const STATUS = {
  lactating: { label: "泌乳中", cls: "green" },
  dry: { label: "干奶", cls: "gray" },
  pregnant: { label: "待产", cls: "blue" },
  sold: { label: "已离场", cls: "gray" },
};
const SESSION = {
  morning: "早班", noon: "午班", evening: "晚班",
};
const DETECTION = { observed: "人工观察", activity: "计步器", detector: "尾根蜡笔" };
const H_TYPE = { checkup: "常规体检", diagnosis: "疾病诊断", vaccination: "免疫接种" };
const SEVERITY = { mild: "轻度", moderate: "中度", severe: "重度" };
const H_RESULT = { recovered: "已康复", ongoing: "治疗中", observed: "观察中" };
const INSEM_RESULT = { pending: "待孕检", pregnant: "已孕", negative: "未孕", unknown: "未确认" };

const REMINDER_META = {
  estrus: { ico: "🔥", label: "发情配种" },
  return_estrus: { ico: "🔄", label: "返情观察" },
  preg_check: { ico: "🤰", label: "妊娠检查" },
  open_cow: { ico: "⚠️", label: "长期空怀" },
  medication_dose: { ico: "💉", label: "续用药" },
  withdrawal: { ico: "🚫", label: "休药期" },
  health_followup: { ico: "🏥", label: "健康复查" },
  calving: { ico: "🐣", label: "待产" },
  first_insemination: { ico: "📅", label: "产后首配" },
};

/* ---------------- 全局状态 ---------------- */
const S = reactive({
  view: "dashboard",
  toasts: [],
  modals: [],
  cows: [],
  drugs: [],
  dashboard: null,
  reminders: [],
  anomalies: [],
  milkings: [],
  health: [],
  meds: [],
  estruses: [],
  loading: { milkings: false },
});

let toastSeq = 0;
function toast(message, type = "success") {
  const id = ++toastSeq;
  S.toasts.push({ id, message, type });
  setTimeout(() => {
    const i = S.toasts.findIndex((t) => t.id === id);
    if (i >= 0) S.toasts.splice(i, 1);
  }, 3800);
}

function openModal(modal) { S.modals.push(modal); }
function closeModal() { S.modals.pop(); }
function topModal() { return S.modals[S.modals.length - 1]; }

async function loadCows() {
  S.cows = await api("/api/cows");
}
async function loadDrugs() {
  S.drugs = await api("/api/drugs");
}
async function loadDashboard() {
  S.dashboard = await api("/api/dashboard");
}
async function loadReminders() {
  S.reminders = await api("/api/reminders");
}
async function loadAnomalies(days = 7) {
  S.anomalies = await api("/api/anomalies?days=" + days);
}

async function switchView(v) {
  S.view = v;
  if (v === "cows" && !S.cows.length) loadCows().catch((e) => toast(e.message, "error"));
  if (v === "milkings") loadMilkings();
  if (v === "health") {
    if (!S.cows.length) loadCows().catch((e) => toast(e.message, "error"));
    if (!S.health.length) loadHealthAll();
    if (!S.meds.length) loadMedsAll();
    if (!S.drugs.length) loadDrugs();
  }
  if (v === "repro") {
    if (!S.cows.length) loadCows().catch((e) => toast(e.message, "error"));
    if (!S.estruses.length) loadEstrusesAll();
  }
  if (v === "milkings" && !S.cows.length) {
    loadCows().catch((e) => toast(e.message, "error"));
  }
}

async function loadMilkings(q = "") {
  S.loading.milkings = true;
  try {
    S.milkings = await api("/api/milkings?limit=300" + q);
  } catch (e) { toast(e.message, "error"); }
  S.loading.milkings = false;
}
async function loadHealthAll() { S.health = await api("/api/health"); }
async function loadMedsAll() { S.meds = await api("/api/medications"); }
async function loadEstrusesAll() { S.estruses = await api("/api/estruses"); }

/* ---------------- 柱状图 ---------------- */
const BarChart = {
  props: ["points", "height"],
  template: `
  <svg class="chart" :style="{ height: (height || 200) + 'px' }" preserveAspectRatio="none">
    <g v-for="(p,i) in points" :key="i">
      <rect :x="i*colW + colW*0.22" :y="scale(p.value)" :width="colW*0.56"
            :height="Math.max(0, H - padB - scale(p.value))" rx="2" fill="#3d8b4f"></rect>
      <rect :x="i*colW + colW*0.22" :y="scale(p.value + (p.extra||0))"
            :width="colW*0.56"
            :height="Math.max(0, scale(p.value) - scale(p.value + (p.extra||0)))"
            fill="#fca5a5" opacity="0.85"></rect>
      <text v-if="(i % 2 === 0)" :x="i*colW + colW/2" :y="H - 6" font-size="9"
            fill="#6b7280" text-anchor="middle">{{ p.label }}</text>
      <text :x="i*colW + colW/2" :y="scale(p.value + (p.extra||0)) - 4" font-size="9"
            fill="#374151" text-anchor="middle">{{ Math.round(p.value + (p.extra||0)) }}</text>
    </g>
</svg>
  `,
  setup(props) {
    const H = computed(() => props.height || 200);
    const padB = 18;
    const colW = computed(() => 100 / Math.max(1, props.points.length) + "%");
    const maxV = computed(() => Math.max(10, ...props.points.map((p) => p.value + (p.extra || 0))) * 1.15);
    const scale = (v) => (H.value - padB) * (1 - v / maxV.value) + 4;
    return { H, padB, colW, scale };
  },
};

/* ---------------- 迷你折线 ---------------- */
const Sparkline = {
  props: ["points"],
  template: `
  <svg class="spark" viewBox="0 0 300 150" preserveAspectRatio="none">
    <polyline :points="line" fill="none" stroke="#3d8b4f" stroke-width="2.5" stroke-linejoin="round"></polyline>
    <g v-for="(p,i) in points" :key="i">
      <circle :cx="i*stepX + padL" :cy="y(p.v)" r="3.5" :fill="p.v === 0 ? '#9ca3af' : '#3d8b4f'"></circle>
      <text :x="i*stepX + padL" :y="y(p.v) - 8" font-size="10" fill="#374151" text-anchor="middle">{{ p.v }}</text>
    </g>
    <text v-for="(p,i) in points" :key="'l'+i" :x="i*stepX + padL" y="146" font-size="9"
          fill="#6b7280" text-anchor="middle">{{ p.label }}</text>
  </svg>`,
  setup(props) {
    const padL = 16;
    const stepX = (300 - padL * 2) / Math.max(1, props.points.length - 1);
    const max = Math.max(5, ...props.points.map((p) => p.v)) * 1.15;
    const y = (v) => 120 - (v / max) * 100;
    const line = props.points.map((p, i) => `${i * stepX + padL},${y(p.v)}`).join(" ");
    return { stepX, y, line, padL };
  },
};

/* ---------------- 工作台 ---------------- */
const Dashboard = {
  components: { BarChart },
  setup() {
    const chartPoints = computed(() =>
      (S.dashboard?.trend_14d || []).map((t) => ({
        label: t.date.slice(5),
        value: t.total_kg,
        extra: t.discarded_kg,
      }))
    );
    const deltaPct = computed(() => {
      const a = S.dashboard?.today_milk.total_kg || 0;
      const b = S.dashboard?.yesterday_milk.total_kg || 0;
      if (!b) return null;
      return Math.round(((a - b) / b) * 100);
    });
    const todayPartial = computed(() => {
      const t = S.dashboard?.trend_14d || [];
      if (t.length < 2) return false;
      return t[t.length - 1].total_kg + t[t.length - 1].discarded_kg <
        (t[t.length - 2].total_kg + t[t.length - 2].discarded_kg) * 0.6;
    });
    return { S, chartPoints, deltaPct, todayPartial, REMINDER_META, fmtSCC };
  },
  template: `
  <div v-if="S.dashboard">
    <div class="grid grid-4">
      <div class="card stat">
        <div class="label">在场牛只</div>
        <div class="value">{{ S.dashboard.cows_active }}<span class="unit"> / {{ S.dashboard.cows_total }} 头</span></div>
        <div class="delta">泌乳 {{ S.dashboard.by_status.lactating || 0 }} · 干奶 {{ S.dashboard.by_status.dry || 0 }} · 待产 {{ S.dashboard.by_status.pregnant || 0 }}</div>
        <div class="big-ico">🐄</div>
      </div>
      <div class="card stat">
        <div class="label">今日产奶（已上市）</div>
        <div class="value">{{ S.dashboard.today_milk.total_kg }}<span class="unit"> kg</span></div>
        <div class="delta" :class="deltaPct == null ? '' : (deltaPct >= 0 ? 'up' : 'down')">
          <template v-if="deltaPct == null">昨日无数据</template>
          <template v-else-if="todayPartial">今日数据尚在记录中，暂不可比</template>
          <template v-else>较昨日 {{ deltaPct >= 0 ? '+' + deltaPct : deltaPct }}%</template>
          · 头日均 {{ S.dashboard.today_milk.avg_kg }}kg
        </div>
        <div class="big-ico">🥛</div>
      </div>
      <div class="card stat alert">
        <div class="label">紧急待办</div>
        <div class="value">{{ S.dashboard.reminder_danger }}<span class="unit"> / {{ S.dashboard.reminder_count }} 条提醒</span></div>
        <div class="delta">休药期牛只 {{ S.dashboard.cows_in_withdrawal }} 头</div>
        <div class="big-ico">🔔</div>
      </div>
      <div class="card stat warn">
        <div class="label">奶量异常牛只</div>
        <div class="value">{{ S.dashboard.anomaly_count }}<span class="unit"> 头</span></div>
        <div class="delta">休药期混装违规 {{ S.dashboard.violation_count }} 条</div>
        <div class="big-ico">📉</div>
      </div>
    </div>

    <div class="grid grid-2" style="margin-top:16px">
      <div class="card">
        <div class="card-title">📈 近14天产奶量
          <span class="sub">绿色=上市奶，红色=废弃奶（今日可能仅早班）</span>
        </div>
        <bar-chart :points="chartPoints"></bar-chart>
        <div class="legend">
          <span><i style="background:#3d8b4f"></i>上市奶 kg</span>
          <span><i style="background:#fca5a5"></i>废弃奶 kg</span>
        </div>
      </div>
      <div class="card">
        <div class="card-title">🔔 今日提醒 <span class="spacer"></span>
          <span class="badge red">{{ S.reminders.length }}</span>
        </div>
        <div class="reminder-list">
          <div v-for="(r,i) in S.reminders" :key="i" class="reminder" :class="r.level">
            <div class="r-ico">{{ REMINDER_META[r.type]?.ico || '•' }}</div>
            <div class="r-body">
              <div class="r-title">{{ r.title }}</div>
              <div class="r-detail">{{ r.detail }}</div>
              <div class="r-meta">
                {{ REMINDER_META[r.type]?.label }} · 截止 {{ r.due_date }}
                <span v-if="r.days_overdue > 0" class="overdue">· 已逾期 {{ r.days_overdue }} 天</span>
                <button class="link" style="margin-left:8px"
                  @click="openModal({type:'cowDetail', id:r.cow_id})">查看牛只</button>
              </div>
            </div>
          </div>
          <div v-if="!S.reminders.length" class="empty">暂无提醒，牛群状态良好 🌿</div>
        </div>
      </div>
    </div>

    <div class="card" style="margin-top:16px">
      <div class="card-title">⚠️ 奶量异常发现 <span class="sub">逐班次对比近7天基线（单班骤降&gt;25% / 体细胞≥50万 / 连续3天下滑）</span></div>
      <div class="table-wrap">
        <table class="data">
          <thead><tr><th>牛只</th><th>风险等级</th><th>异常类型</th><th>说明</th><th>最近日期</th><th></th></tr></thead>
          <tbody>
            <tr v-for="a in S.anomalies" :key="a.cow_id">
              <td><b>{{ a.cow_tag }}</b> {{ a.cow_name ? '（' + a.cow_name + '）' : '' }}</td>
              <td><span class="badge" :class="{red:a.level==='danger',amber:a.level==='warning',blue:a.level==='info'}">
                {{ {danger:'高风险',warning:'需关注',info:'观察'}[a.level] }}</span></td>
              <td><span v-for="t in a.tag_labels" :key="t" class="badge gray" style="margin-right:4px">{{ t }}</span></td>
              <td style="max-width:420px">{{ a.message.replace(a.cow_tag + (a.cow_name ? '（'+a.cow_name+'）' : '') + '：','') }}</td>
              <td>{{ a.latest_date }}</td>
              <td><button class="link" @click="openModal({type:'cowDetail', id:a.cow_id})">档案/处置</button></td>
            </tr>
            <tr v-if="!S.anomalies.length"><td colspan="6" class="empty">近7天未发现异常 ✅</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>`,
};

/* ---------------- 奶牛档案 ---------------- */
const CowsPage = {
  setup() {
    const q = ref("");
    const status = ref("");
    const filtered = computed(() => S.cows);
    async function reload() {
      const p = new URLSearchParams();
      if (q.value) p.set("q", q.value);
      if (status.value) p.set("status", status.value);
      S.cows = await api("/api/cows?" + p.toString());
    }
    const age = (b) => {
      const days = Math.floor((Date.now() - new Date(b).getTime()) / 86400000);
      return (days / 365).toFixed(1) + " 岁";
    };
    return { S, q, status, filtered, reload, openModal, STATUS, age };
  },
  template: `
  <div class="card">
    <div class="toolbar">
      <div class="search"><span class="ico">🔍</span>
        <input class="input" placeholder="搜索耳标号/牛名/牛舍" v-model="q" @keyup.enter="reload">
      </div>
      <select class="input" style="width:130px" v-model="status" @change="reload">
        <option value="">全部状态</option>
        <option value="lactating">泌乳中</option>
        <option value="dry">干奶</option>
        <option value="pregnant">待产</option>
        <option value="sold">已离场</option>
      </select>
      <span class="spacer"></span>
      <button class="btn btn-primary" @click="openModal({type:'cowForm', cow:null})">＋ 新建档案</button>
    </div>
    <div class="table-wrap">
      <table class="data">
        <thead><tr>
          <th>耳标号</th><th>牛名</th><th>品种</th><th>胎次</th><th>年龄</th>
          <th>状态</th><th>牛舍</th><th>最近产犊/泌乳天数</th><th>预产期</th>
          <th class="num">标定日产kg</th><th>操作</th>
        </tr></thead>
        <tbody>
          <tr v-for="c in S.cows" :key="c.id">
            <td><b>{{ c.ear_tag }}</b></td>
            <td>{{ c.name || '-' }}</td>
            <td>{{ c.breed }}</td>
            <td>{{ c.parity }}</td>
            <td>{{ age(c.birth_date) }}</td>
            <td><span class="badge" :class="STATUS[c.status].cls">{{ STATUS[c.status].label }}</span></td>
            <td>{{ c.group || '-' }}</td>
            <td>
              <template v-if="c.calving_date">{{ c.calving_date }} ·
                <span class="badge gray">DIM {{ Math.floor((Date.now()-new Date(c.calving_date).getTime())/86400000) }}</span>
              </template>
              <span v-else>-</span>
            </td>
            <td>{{ c.expected_calving_date || '-' }}</td>
            <td class="num">{{ c.avg_yield_kg ?? '-' }}</td>
            <td style="white-space:nowrap">
              <button class="link" style="margin-right:10px" @click="openModal({type:'cowDetail', id:c.id})">详情</button>
              <button class="link" style="margin-right:10px" @click="openModal({type:'cowForm', cow:c})">编辑</button>
              <button class="link" style="color:#dc2626" @click="removeCow(c)">删除</button>
            </td>
          </tr>
          <tr v-if="!S.cows.length"><td colspan="11" class="empty">没有符合条件的牛只</td></tr>
        </tbody>
      </table>
    </div>
  </div>`,
  methods: {
    async removeCow(c) {
      if (!confirm(`确认删除 ${c.ear_tag} 的档案？其全部关联记录将一并删除。`)) return;
      try {
        await api(`/api/cows/${c.id}`, { method: "DELETE" });
        toast("档案已删除");
        S.cows = S.cows.filter((x) => x.id !== c.id);
      } catch (e) { toast(e.message, "error"); }
    },
  },
};

/* ---------------- 挤奶记录 ---------------- */
const MilkingsPage = {
  setup() {
    const dateFrom = ref(addDays(todayStr(), -6));
    const dateTo = ref(todayStr());
    const cowId = ref("");
    const onlyV = ref(false);
    async function reload() {
      const p = new URLSearchParams();
      // 仅看违规时跨全部历史筛查，避免日期窗口漏掉较早的违规记录
      if (!onlyV.value) {
        p.set("date_from", dateFrom.value);
        p.set("date_to", dateTo.value);
      }
      if (cowId.value) p.set("cow_id", cowId.value);
      if (onlyV.value) {
        p.set("only_violations", "true");
        p.set("limit", "1000");
      }
      await loadMilkings("&" + p.toString());
    }
    const lactatingCows = computed(() => S.cows.filter((c) => c.status === "lactating"));
    return { S, dateFrom, dateTo, cowId, onlyV, reload, openModal, SESSION, lactatingCows };
  },
  template: `
  <div class="card">
    <div class="toolbar">
      <label class="input" :style="{width:'auto',display:'flex',alignItems:'center',gap:'6px',borderStyle:'dashed',opacity: onlyV ? .45 : 1}">
        起 <input type="date" class="input" style="border:none;width:130px;padding:2px"
              :disabled="onlyV" v-model="dateFrom" @change="reload">
      </label>
      <label class="input" :style="{width:'auto',display:'flex',alignItems:'center',gap:'6px',borderStyle:'dashed',opacity: onlyV ? .45 : 1}">
        止 <input type="date" class="input" style="border:none;width:130px;padding:2px"
              :disabled="onlyV" v-model="dateTo" @change="reload">
      </label>
      <select class="input" style="width:160px" v-model="cowId" @change="reload">
        <option value="">全部牛只</option>
        <option v-for="c in lactatingCows" :key="c.id" :value="c.id">{{ c.ear_tag }} {{ c.name || '' }}</option>
      </select>
      <label style="display:flex;align-items:center;gap:6px;color:#b45309;font-weight:600;cursor:pointer">
        <input type="checkbox" v-model="onlyV" @change="reload"> 仅看休药期违规混装
        <span v-if="onlyV" style="color:#6b7280;font-weight:400">（全历史筛查，最多1000条）</span>
      </label>
      <span class="spacer"></span>
      <button class="btn btn-primary" @click="openModal({type:'milkingForm', rec:null})">＋ 登记挤奶</button>
    </div>
    <div class="table-wrap">
      <table class="data">
        <thead><tr>
          <th>日期</th><th>班次</th><th>耳标号</th><th>牛名</th>
          <th class="num">产奶量 kg</th><th class="num">体细胞 cells/mL</th>
          <th>处置/校验</th><th>备注</th><th>操作</th>
        </tr></thead>
        <tbody>
          <tr v-for="r in S.milkings" :key="r.id" :style="r.violation ? 'background:#fff5f5' : ''">
            <td>{{ r.date }}</td>
            <td>{{ SESSION[r.session] }}</td>
            <td><b>{{ r.cow_ear_tag }}</b></td>
            <td>{{ r.cow_name || '-' }}</td>
            <td class="num" :style="r.discarded ? 'text-decoration:line-through;color:#9ca3af' : ''">{{ r.yield_kg }}</td>
            <td class="num">
              <span :class="r.scc >= 500000 ? 'badge red' : ''">{{ r.scc ?? '-' }}</span>
            </td>
            <td>
              <span v-if="r.violation" class="badge red">🚨 休药期违规混装</span>
              <span v-else-if="r.discarded" class="badge amber">🚫 已废弃</span>
              <span v-else-if="r.in_withdrawal" class="badge amber">休药期内</span>
              <span v-else class="badge green">✅ 正常上市</span>
            </td>
            <td style="max-width:180px;color:#6b7280">{{ r.note || '-' }}</td>
            <td style="white-space:nowrap">
              <button v-if="r.violation" class="btn btn-sm btn-danger" style="margin-right:8px"
                @click="markDiscard(r)">标记废弃</button>
              <button class="link" style="margin-right:10px" @click="openModal({type:'milkingForm', rec:r})">编辑</button>
              <button class="link" style="color:#dc2626" @click="remove(r)">删除</button>
            </td>
          </tr>
          <tr v-if="!S.milkings.length"><td colspan="9" class="empty">暂无记录</td></tr>
        </tbody>
      </table>
    </div>
  </div>`,
  methods: {
    async markDiscard(r) {
      try {
        await api(`/api/milkings/${r.id}`, { method: "PATCH", body: { discarded: true } });
        toast(`已将 ${r.cow_ear_tag} ${r.date}${this.SESSION[r.session]} 奶标记废弃`);
        await this.reload();
      } catch (e) { toast(e.message, "error"); }
    },
    async remove(r) {
      if (!confirm("确认删除该挤奶记录？")) return;
      try {
        await api(`/api/milkings/${r.id}`, { method: "DELETE" });
        toast("已删除");
        await this.reload();
      } catch (e) { toast(e.message, "error"); }
    },
  },
};

/* ---------------- 健康与用药 ---------------- */
const HealthPage = {
  setup() {
    const tab = ref("health");
    return { S, tab, openModal, H_TYPE, SEVERITY, H_RESULT };
  },
  template: `
  <div class="card">
    <div class="tabs">
      <button class="tab" :class="{active:tab==='health'}" @click="tab='health'">🏥 健康记录</button>
      <button class="tab" :class="{active:tab==='meds'}" @click="tab='meds'">💊 用药记录</button>
      <button class="tab" :class="{active:tab==='drugs'}" @click="tab='drugs'">📖 药品目录与休药期</button>
    </div>

    <div v-if="tab==='health'">
      <div class="toolbar">
        <span class="spacer"></span>
        <button class="btn btn-primary" @click="openModal({type:'healthForm', rec:null})">＋ 登记健康记录</button>
      </div>
      <div class="table-wrap"><table class="data">
        <thead><tr><th>日期</th><th>耳标号</th><th>类型</th><th>诊断/项目</th><th>体温</th><th>程度</th><th>复查日</th><th>状态</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="h in S.health" :key="h.id">
            <td>{{ h.date }}</td><td><b>{{ h.cow_ear_tag }}</b> {{ h.cow_name || '' }}</td>
            <td>{{ H_TYPE[h.record_type] }}</td>
            <td>{{ h.diagnosis || '-' }}</td>
            <td><span :class="h.temperature >= 39.5 ? 'badge red' : ''">{{ h.temperature ? h.temperature + '℃' : '-' }}</span></td>
            <td><span v-if="h.severity" class="badge" :class="{'gray':h.severity==='mild','amber':h.severity==='moderate','red':h.severity==='severe'}">{{ SEVERITY[h.severity] }}</span><span v-else>-</span></td>
            <td>{{ h.follow_up_date || '-' }}</td>
            <td><span class="badge" :class="{'green':h.result==='recovered','amber':h.result==='ongoing','blue':h.result==='observed','gray':!h.result}">{{ H_RESULT[h.result] || '未结案' }}</span></td>
            <td style="white-space:nowrap">
              <button class="link" style="margin-right:10px" @click="openModal({type:'healthForm', rec:h})">编辑</button>
              <button class="link" style="color:#dc2626" @click="removeHealth(h)">删除</button>
            </td>
          </tr>
          <tr v-if="!S.health.length"><td colspan="9" class="empty">暂无健康记录</td></tr>
        </tbody>
      </table></div>
    </div>

    <div v-if="tab==='meds'">
      <div class="toolbar">
        <span class="spacer"></span>
        <button class="btn btn-primary" @click="openModal({type:'medForm', rec:null})">＋ 登记用药</button>
      </div>
      <div class="table-wrap"><table class="data">
        <thead><tr><th>用药日期</th><th>耳标号</th><th>药品</th><th>剂量</th><th>途径</th><th>原因</th>
          <th>休药期</th><th>鲜奶可售日</th><th>下次用药</th><th>兽医</th><th>操作</th></tr></thead>
        <tbody>
          <tr v-for="m in S.meds" :key="m.id" :style="m.active_withdrawal ? 'background:#fffbeb' : ''">
            <td>{{ m.date }}</td>
            <td><b @click="openModal({type:'cowDetail', id:m.cow_id})" class="link">{{ cowTag(m.cow_id) }}</b></td>
            <td>{{ m.drug_name }}<span v-if="m.active_withdrawal" class="badge red" style="margin-left:6px">休药中</span></td>
            <td>{{ m.dose || '-' }}</td><td>{{ m.route || '-' }}</td><td>{{ m.reason || '-' }}</td>
            <td class="num">{{ m.withdrawal_days }} 天</td>
            <td><b>{{ addDays(m.withdrawal_end, 1) }}</b></td>
            <td>
              <template v-if="m.next_dose_date">
                {{ m.next_dose_date }}
                <span v-if="!m.treated" class="badge amber" style="margin-left:4px">待执行</span>
              </template>
              <span v-else>-</span>
            </td>
            <td>{{ m.operator || '-' }}</td>
            <td style="white-space:nowrap">
              <button v-if="m.next_dose_date && !m.treated" class="btn btn-sm" style="margin-right:8px"
                @click="doneDose(m)">已执行</button>
              <button class="link" style="color:#dc2626" @click="removeMed(m)">删除</button>
            </td>
          </tr>
          <tr v-if="!S.meds.length"><td colspan="11" class="empty">暂无用药记录</td></tr>
        </tbody>
      </table></div>
    </div>

    <div v-if="tab==='drugs'">
      <div class="toolbar">
        <span style="color:#6b7280;font-size:12.5px">登记用药时选择药品将自动套用默认牛奶休药期；休药期含用药当天，结束日次日方可上市。</span>
        <span class="spacer"></span>
        <button class="btn btn-primary" @click="openModal({type:'drugForm'})">＋ 新增药品</button>
      </div>
      <div class="table-wrap"><table class="data">
        <thead><tr><th>药品名称</th><th>类别</th><th class="num">默认休药期(天)</th><th>鲜奶可售日(用药后)</th><th>备注</th></tr></thead>
        <tbody>
          <tr v-for="d in S.drugs" :key="d.id">
            <td><b>{{ d.name }}</b></td><td>{{ d.usage || '-' }}</td>
            <td class="num"><span class="badge" :class="d.default_withdrawal_days >= 7 ? 'red' : (d.default_withdrawal_days > 0 ? 'amber' : 'green')">{{ d.default_withdrawal_days }}</span></td>
            <td>{{ d.default_withdrawal_days === 0 ? '当天可售' : '第 ' + (d.default_withdrawal_days + 1) + ' 天' }}</td>
            <td style="color:#6b7280">{{ d.note || '-' }}</td>
          </tr>
        </tbody>
      </table></div>
    </div>
  </div>`,
  methods: {
    addDays,
    cowTag(id) { const c = S.cows.find((x) => x.id === id); return c ? c.ear_tag : id; },
    async doneDose(m) {
      try {
        await api(`/api/medications/${m.id}`, { method: "PATCH", body: { treated: true } });
        toast("已标记为执行，提醒将关闭");
        await loadMedsAll();
      } catch (e) { toast(e.message, "error"); }
    },
    async removeHealth(h) {
      if (!confirm("确认删除该健康记录？")) return;
      await api(`/api/health/${h.id}`, { method: "DELETE" });
      S.health = S.health.filter((x) => x.id !== h.id);
      toast("已删除");
    },
    async removeMed(m) {
      if (!confirm("确认删除该用药记录？休药期校验将立即失效。")) return;
      await api(`/api/medications/${m.id}`, { method: "DELETE" });
      S.meds = S.meds.filter((x) => x.id !== m.id);
      toast("已删除");
    },
  },
};

/* ---------------- 发情与配种 ---------------- */
const ReproPage = {
  setup() { return { S, openModal, DETECTION, INSEM_RESULT }; },
  template: `
  <div class="card">
    <div class="toolbar">
      <span style="color:#6b7280;font-size:12.5px">发情发现后 12 小时内为最佳输精窗口；配种后 18~24 天观察返情、35~42 天进行孕检。</span>
      <span class="spacer"></span>
      <button class="btn btn-primary" @click="openModal({type:'estrusForm', rec:null})">＋ 登记发情/配种</button>
    </div>
    <div class="table-wrap"><table class="data">
      <thead><tr><th>发情日期</th><th>耳标号</th><th>发现方式</th><th>强度</th><th>配种</th>
        <th>冻精/公牛</th><th>配种员</th><th>孕检结果</th><th>备注</th><th>操作</th></tr></thead>
      <tbody>
        <tr v-for="e in S.estruses" :key="e.id">
          <td>{{ e.date }}</td>
          <td><b @click="openModal({type:'cowDetail', id:e.cow_id})" class="link">{{ e.cow_ear_tag }}</b> {{ e.cow_name || '' }}</td>
          <td>{{ DETECTION[e.detection] }}</td>
          <td>{{ e.score ? '★'.repeat(e.score) : '-' }}</td>
          <td>
            <span v-if="e.inseminated" class="badge green">已配 {{ e.insemination_date }}</span>
            <span v-else class="badge red">待配种</span>
          </td>
          <td>{{ e.semen || '-' }}</td><td>{{ e.technician || '-' }}</td>
          <td>
            <select class="input" style="padding:4px 8px;width:100px"
                    :value="e.result || 'pending'" @change="setResult(e, $event.target.value)">
              <option value="pending">待孕检</option>
              <option value="pregnant">已孕</option>
              <option value="negative">未孕</option>
              <option value="unknown">未确认</option>
            </select>
          </td>
          <td style="max-width:180px;color:#6b7280">{{ e.note || '-' }}</td>
          <td style="white-space:nowrap">
            <button class="link" style="margin-right:10px" @click="openModal({type:'estrusForm', rec:e})">编辑</button>
            <button class="link" style="color:#dc2626" @click="remove(e)">删除</button>
          </td>
        </tr>
        <tr v-if="!S.estruses.length"><td colspan="10" class="empty">暂无发情/配种记录</td></tr>
      </tbody>
    </table></div>
  </div>`,
  methods: {
    async setResult(e, result) {
      try {
        const body = { result };
        if (result !== "pending") body.result_date = todayStr();
        await api(`/api/estruses/${e.id}`, { method: "PATCH", body });
        e.result = result;
        if (result !== "pending") e.result_date = todayStr();
        toast("孕检结果已更新");
      } catch (err) { toast(err.message, "error"); }
    },
    async remove(e) {
      if (!confirm("确认删除该发情/配种记录？")) return;
      await api(`/api/estruses/${e.id}`, { method: "DELETE" });
      S.estruses = S.estruses.filter((x) => x.id !== e.id);
      toast("已删除");
    },
  },
};

/* ---------------- 弹窗：奶牛表单 ---------------- */
const CowFormModal = {
  setup() {
    const m = topModal();
    const f = reactive(
      m.cow
        ? { ...m.cow, birth_date: m.cow.birth_date?.slice(0, 10),
            calving_date: m.cow.calving_date?.slice(0, 10) || null,
            expected_calving_date: m.cow.expected_calving_date?.slice(0, 10) || null }
        : { ear_tag: "", name: "", breed: "荷斯坦牛", birth_date: todayStr(), parity: 1,
            status: "lactating", group: "", calving_date: null,
            expected_calving_date: null, avg_yield_kg: null, note: "" }
    );
    const err = ref("");
    async function save() {
      err.value = "";
      try {
        if (m.cow) {
          await api(`/api/cows/${m.cow.id}`, { method: "PATCH", body: { ...f } });
          toast("档案已更新");
        } else {
          await api("/api/cows", { method: "POST", body: f });
          toast("档案已创建");
        }
        await loadCows();
        closeModal();
      } catch (e) { err.value = e.message; }
    }
    return { f, err, save, closeModal };
  },
  template: `
  <div class="modal-mask" @click.self="closeModal"><div class="modal">
    <div class="modal-head"><h3>奶牛档案</h3><button class="modal-close" @click="closeModal">×</button></div>
    <div class="modal-body">
      <div class="alert-box danger" v-if="err">{{ err }}</div>
      <div class="field-row">
        <div class="field"><label>耳标号 <span class="req">*</span></label>
          <input class="input" v-model="f.ear_tag" :disabled="false" placeholder="如 1613"></div>
        <div class="field"><label>牛名</label><input class="input" v-model="f.name"></div>
      </div>
      <div class="field-row">
        <div class="field"><label>品种</label>
          <select class="input" v-model="f.breed">
            <option>荷斯坦牛</option><option>娟姗牛</option><option>瑞士褐牛</option>
            <option>西门塔尔牛</option><option>三河牛</option>
          </select></div>
        <div class="field"><label>胎次</label><input type="number" min="0" max="20" class="input" v-model.number="f.parity"></div>
      </div>
      <div class="field-row">
        <div class="field"><label>出生日期</label><input type="date" class="input" v-model="f.birth_date"></div>
        <div class="field"><label>状态</label>
          <select class="input" v-model="f.status">
            <option value="lactating">泌乳中</option><option value="dry">干奶</option>
            <option value="pregnant">待产</option><option value="sold">已离场</option>
          </select></div>
      </div>
      <div class="field-row">
        <div class="field"><label>牛舍/群组</label><input class="input" v-model="f.group" placeholder="如 A栋1栏"></div>
        <div class="field"><label>标定日产奶量 kg</label><input type="number" step="0.1" min="0" class="input" v-model.number="f.avg_yield_kg"></div>
      </div>
      <div class="field-row">
        <div class="field"><label>最近产犊日期</label><input type="date" class="input" v-model="f.calving_date"></div>
        <div class="field"><label>预产期</label><input type="date" class="input" v-model="f.expected_calving_date"></div>
      </div>
      <div class="field"><label>备注</label><textarea class="input" rows="2" v-model="f.note"></textarea></div>
    </div>
    <div class="modal-foot">
      <button class="btn" @click="closeModal">取消</button>
      <button class="btn btn-primary" @click="save">保存</button>
    </div>
  </div></div>`,
};

/* ---------------- 弹窗：挤奶表单（含休药期校验） ---------------- */
const MilkingFormModal = {
  setup() {
    const m = topModal();
    const f = reactive(
      m.rec
        ? { ...m.rec }
        : { cow_id: null, date: todayStr(), session: "morning", yield_kg: null, scc: null, note: "" }
    );
    const err = ref("");
    const check = ref(null);
    const lactating = computed(() => S.cows.filter((c) => c.status === "lactating"));
    async function runCheck() {
      check.value = null;
      if (!f.cow_id || !f.date) return;
      try {
        check.value = await api("/api/milkings/check-withdrawal", {
          method: "POST", body: { cow_id: f.cow_id, date: f.date },
        });
      } catch (e) { /* ignore */ }
    }
    async function save() {
      err.value = "";
      try {
        let res;
        if (m.rec) {
          res = await api(`/api/milkings/${m.rec.id}`, {
            method: "PATCH",
            body: { session: f.session, yield_kg: f.yield_kg, scc: f.scc, note: f.note, discarded: f.discarded },
          });
          toast("记录已更新");
        } else {
          res = await api("/api/milkings", { method: "POST", body: f });
          if (res.warnings?.length) toast(res.warnings[0].message, "warn");
          else toast("挤奶记录已登记");
        }
        closeModal();
        if (S.view === "milkings") await loadMilkings();
        refreshDash();
      } catch (e) { err.value = e.message; }
    }
    nextTick(runCheck);
    return { f, err, check, save, closeModal, runCheck, lactating, SESSION };
  },
  template: `
  <div class="modal-mask" @click.self="closeModal"><div class="modal">
    <div class="modal-head"><h3>登记挤奶记录</h3><button class="modal-close" @click="closeModal">×</button></div>
    <div class="modal-body">
      <div class="alert-box danger" v-if="err">{{ err }}</div>
      <div class="alert-box warn" v-if="check && check.in_withdrawal">
        🚫 {{ check.message }} —— 保存时将<strong>自动标记为废弃奶</strong>，不计入大罐产量。
      </div>
      <div class="alert-box info" v-if="check && !check.in_withdrawal">✅ {{ check.message }}</div>
      <div class="field-row">
        <div class="field"><label>牛只 <span class="req">*</span></label>
          <select class="input" v-model="f.cow_id" @change="runCheck" :disabled="!!f.id">
            <option :value="null" disabled>请选择</option>
            <option v-for="c in lactating" :key="c.id" :value="c.id">{{ c.ear_tag }} {{ c.name || '' }}（{{ c.group || '' }}）</option>
          </select></div>
        <div class="field"><label>日期 <span class="req">*</span></label>
          <input type="date" class="input" v-model="f.date" @change="runCheck" :disabled="!!f.id"></div>
      </div>
      <div class="field-row">
        <div class="field"><label>班次</label>
          <select class="input" v-model="f.session">
            <option value="morning">早班</option><option value="noon">午班</option><option value="evening">晚班</option>
          </select></div>
        <div class="field"><label>产奶量 kg <span class="req">*</span></label>
          <input type="number" step="0.1" min="0" class="input" v-model.number="f.yield_kg"></div>
      </div>
      <div class="field"><label>体细胞数 SCC（cells/mL，选填）</label>
        <input type="number" min="0" step="1000" class="input" v-model.number="f.scc" placeholder="健康牛通常 &lt; 30 万">
        <div class="hint">≥ 50 万将触发乳房炎风险提示</div></div>
      <div class="field" v-if="f.id"><label><input type="checkbox" v-model="f.discarded"> 该批奶废弃不计入上市奶</label></div>
      <div class="field"><label>备注</label><textarea class="input" rows="2" v-model="f.note"></textarea></div>
    </div>
    <div class="modal-foot">
      <button class="btn" @click="closeModal">取消</button>
      <button class="btn btn-primary" @click="save">保存</button>
    </div>
  </div></div>`,
};

async function refreshDash() {
  try { await Promise.all([loadDashboard(), loadAnomalies(), loadReminders()]); } catch (_) {}
}

/* ---------------- 弹窗：健康表单 ---------------- */
const HealthFormModal = {
  setup() {
    const m = topModal();
    const f = reactive(
      m.rec?.id ? { ...m.rec }
        : { cow_id: m.rec?.presetCow || null, date: todayStr(), record_type: "checkup",
            diagnosis: "", temperature: null, severity: null, follow_up_date: null,
            result: null, note: "" }
    );
    const err = ref("");
    async function save() {
      err.value = "";
      try {
        const body = { ...f };
        if (m.rec?.id) {
          delete body.id; delete body.cow_ear_tag; delete body.cow_name;
          await api(`/api/health/${m.rec.id}`, { method: "PATCH", body });
        } else {
          await api("/api/health", { method: "POST", body });
        }
        toast("健康记录已保存（复查日将生成提醒）");
        await loadHealthAll();
        closeModal();
      } catch (e) { err.value = e.message; }
    }
    return { S: S, f, err, save, closeModal, H_TYPE, SEVERITY, H_RESULT };
  },
  template: `
  <div class="modal-mask" @click.self="closeModal"><div class="modal">
    <div class="modal-head"><h3>健康记录</h3><button class="modal-close" @click="closeModal">×</button></div>
    <div class="modal-body">
      <div class="alert-box danger" v-if="err">{{ err }}</div>
      <div class="field-row">
        <div class="field"><label>牛只 <span class="req">*</span></label>
          <select class="input" v-model="f.cow_id" :disabled="!!f.id">
            <option :value="null" disabled>请选择</option>
            <option v-for="c in S.cows.filter(x=>x.status!=='sold')" :key="c.id" :value="c.id">{{ c.ear_tag }} {{ c.name || '' }}</option>
          </select></div>
        <div class="field"><label>日期</label><input type="date" class="input" v-model="f.date"></div>
      </div>
      <div class="field-row">
        <div class="field"><label>记录类型</label>
          <select class="input" v-model="f.record_type">
            <option value="checkup">常规体检</option><option value="diagnosis">疾病诊断</option>
            <option value="vaccination">免疫接种</option>
          </select></div>
        <div class="field"><label>体温 ℃</label><input type="number" step="0.1" class="input" v-model.number="f.temperature" placeholder="正常 38.5~39.3"></div>
      </div>
      <div class="field"><label>诊断 / 项目</label>
        <input class="input" v-model="f.diagnosis" list="diag-list" placeholder="如 临床型乳房炎">
        <datalist id="diag-list">
          <option value="临床型乳房炎"><option value="隐性乳房炎"><option value="产后子宫炎">
          <option value="酮病"><option value="瘤胃酸中毒"><option value="蹄叶炎">
          <option value="呼吸道感染"><option value="口蹄疫O型灭活疫苗">
        </datalist></div>
      <div class="field-row">
        <div class="field"><label>严重程度</label>
          <select class="input" v-model="f.severity">
            <option :value="null">无</option><option value="mild">轻度</option>
            <option value="moderate">中度</option><option value="severe">重度</option>
          </select></div>
        <div class="field"><label>复查日期</label><input type="date" class="input" v-model="f.follow_up_date"></div>
      </div>
      <div class="field"><label>处置状态</label>
        <select class="input" v-model="f.result">
          <option :value="null">未结案</option><option value="observed">观察中</option>
          <option value="ongoing">治疗中</option><option value="recovered">已康复</option>
        </select></div>
      <div class="field"><label>备注</label><textarea class="input" rows="2" v-model="f.note"></textarea></div>
    </div>
    <div class="modal-foot">
      <button class="btn" @click="closeModal">取消</button>
      <button class="btn btn-primary" @click="save">保存</button>
    </div>
  </div></div>`,
};

/* ---------------- 弹窗：药品表单 ---------------- */
const DrugFormModal = {
  setup() {
    const f = reactive({ name: "", usage: "", default_withdrawal_days: 0, note: "" });
    const err = ref("");
    async function save() {
      try {
        await api("/api/drugs", { method: "POST", body: f });
        toast("药品已添加");
        await loadDrugs();
        closeModal();
      } catch (e) { err.value = e.message; }
    }
    return { f, err, save, closeModal };
  },
  template: `
  <div class="modal-mask" @click.self="closeModal"><div class="modal" style="width:480px">
    <div class="modal-head"><h3>新增药品</h3><button class="modal-close" @click="closeModal">×</button></div>
    <div class="modal-body">
      <div class="alert-box danger" v-if="err">{{ err }}</div>
      <div class="field"><label>药品名称 <span class="req">*</span></label><input class="input" v-model="f.name"></div>
      <div class="field"><label>类别/用途</label><input class="input" v-model="f.usage" placeholder="如 抗生素 / 激素 / 驱虫药"></div>
      <div class="field"><label>默认牛奶休药期（天）</label>
        <input type="number" min="0" max="365" class="input" v-model.number="f.default_withdrawal_days">
        <div class="hint">0 表示用药当天鲜奶即可上市；休药期含用药当天</div></div>
      <div class="field"><label>备注</label><textarea class="input" rows="2" v-model="f.note"></textarea></div>
    </div>
    <div class="modal-foot">
      <button class="btn" @click="closeModal">取消</button>
      <button class="btn btn-primary" @click="save">保存</button>
    </div>
  </div></div>`,
};

/* ---------------- 弹窗：用药表单（自动休药期） ---------------- */
const MedFormModal = {
  setup() {
    const m = topModal();
    const f = reactive({
      cow_id: m.rec?.presetCow || null, drug_id: null, drug_name: "",
      date: todayStr(), dose: "", route: "颈部肌注", reason: "",
      withdrawal_days: null, next_dose_date: null, operator: "", note: "",
    });
    const err = ref("");
    const wdEnd = computed(() =>
      f.date && f.withdrawal_days != null ? addDays(f.date, f.withdrawal_days) : null);
    const saleDate = computed(() =>
      wdEnd.value ? addDays(wdEnd.value, 1) : null);
    function pickDrug() {
      const d = S.drugs.find((x) => x.id === f.drug_id);
      if (d) { f.drug_name = d.name; f.withdrawal_days = d.default_withdrawal_days; }
    }
    async function save() {
      err.value = "";
      try {
        await api("/api/medications", { method: "POST", body: f });
        toast("用药记录已保存，休药期校验已生效");
        await loadMedsAll();
        closeModal();
        refreshDash();
      } catch (e) { err.value = e.message; }
    }
    return { S, f, err, save, closeModal, pickDrug, wdEnd, saleDate };
  },
  template: `
  <div class="modal-mask" @click.self="closeModal"><div class="modal">
    <div class="modal-head"><h3>登记用药</h3><button class="modal-close" @click="closeModal">×</button></div>
    <div class="modal-body">
      <div class="alert-box danger" v-if="err">{{ err }}</div>
      <div class="field-row">
        <div class="field"><label>牛只 <span class="req">*</span></label>
          <select class="input" v-model="f.cow_id">
            <option :value="null" disabled>请选择</option>
            <option v-for="c in S.cows.filter(x=>x.status!=='sold')" :key="c.id" :value="c.id">{{ c.ear_tag }} {{ c.name || '' }}</option>
          </select></div>
        <div class="field"><label>用药日期</label><input type="date" class="input" v-model="f.date"></div>
      </div>
      <div class="field"><label>药品（选择后自动套用默认休药期）</label>
        <select class="input" v-model="f.drug_id" @change="pickDrug">
          <option :value="null">— 自定义输入药品名 —</option>
          <option v-for="d in S.drugs" :key="d.id" :value="d.id">
            {{ d.name }}（休药期 {{ d.default_withdrawal_days }} 天）
          </option>
        </select></div>
      <div class="field-row">
        <div class="field"><label>药品名称 <span class="req">*</span></label><input class="input" v-model="f.drug_name"></div>
        <div class="field"><label>休药期（天，可覆盖）</label>
          <input type="number" min="0" max="365" class="input" v-model.number="f.withdrawal_days"></div>
      </div>
      <div class="alert-box" :class="f.withdrawal_days > 0 ? 'warn' : 'info'">
        <template v-if="f.withdrawal_days > 0">
          🚫 休药期 <b>{{ f.date }}</b> 至 <b>{{ wdEnd }}</b>（含当天），鲜奶自
          <b>{{ saleDate }}</b> 起可上市；期间挤奶默认废弃并拦截违规混装。
        </template>
        <template v-else>✅ 休药期 0 天，用药当天鲜奶可上市。</template>
      </div>
      <div class="field-row">
        <div class="field"><label>剂量</label><input class="input" v-model="f.dose" placeholder="如 1g/次，每日1次，连用3日"></div>
        <div class="field"><label>给药途径</label>
          <select class="input" v-model="f.route">
            <option>颈部肌注</option><option>静注</option><option>皮下注射</option>
            <option>乳头灌注</option><option>口服</option><option>外用</option>
          </select></div>
      </div>
      <div class="field-row">
        <div class="field"><label>用药原因</label><input class="input" v-model="f.reason"></div>
        <div class="field"><label>下次用药日期（将生成提醒）</label>
          <input type="date" class="input" v-model="f.next_dose_date" :min="f.date"></div>
      </div>
      <div class="field-row">
        <div class="field"><label>兽医/操作人</label><input class="input" v-model="f.operator"></div>
        <div class="field"><label>备注</label><input class="input" v-model="f.note"></div>
      </div>
    </div>
    <div class="modal-foot">
      <button class="btn" @click="closeModal">取消</button>
      <button class="btn btn-primary" @click="save">保存</button>
    </div>
  </div></div>`,
};

/* ---------------- 弹窗：发情/配种表单 ---------------- */
const EstrusFormModal = {
  setup() {
    const m = topModal();
    const f = reactive(
      m.rec?.id
        ? { ...m.rec }
        : { cow_id: m.rec?.presetCow || null, date: todayStr(), detection: "observed",
            score: 3, inseminated: false, insemination_date: null, semen: "",
            technician: "", result: null, result_date: null, note: "" }
    );
    const err = ref("");
    async function save() {
      err.value = "";
      try {
        if (m.rec?.id) {
          const body = { ...f };
          delete body.id; delete body.cow_ear_tag; delete body.cow_name;
          await api(`/api/estruses/${m.rec.id}`, { method: "PATCH", body });
        } else {
          await api("/api/estruses", { method: "POST", body: f });
        }
        toast("发情/配种记录已保存");
        await loadEstrusesAll();
        closeModal();
        refreshDash();
      } catch (e) { err.value = e.message; }
    }
    return { S, f, err, save, closeModal, DETECTION, INSEM_RESULT };
  },
  template: `
  <div class="modal-mask" @click.self="closeModal"><div class="modal">
    <div class="modal-head"><h3>发情 / 配种记录</h3><button class="modal-close" @click="closeModal">×</button></div>
    <div class="modal-body">
      <div class="alert-box danger" v-if="err">{{ err }}</div>
      <div class="field-row">
        <div class="field"><label>牛只 <span class="req">*</span></label>
          <select class="input" v-model="f.cow_id" :disabled="!!f.id">
            <option :value="null" disabled>请选择</option>
            <option v-for="c in S.cows.filter(x=>['lactating','dry'].includes(x.status))" :key="c.id" :value="c.id">
              {{ c.ear_tag }} {{ c.name || '' }}
            </option>
          </select></div>
        <div class="field"><label>发情日期</label><input type="date" class="input" v-model="f.date"></div>
      </div>
      <div class="field-row">
        <div class="field"><label>发现方式</label>
          <select class="input" v-model="f.detection">
            <option value="observed">人工观察</option><option value="activity">计步器活动量</option>
            <option value="detector">尾根蜡笔/检测器</option>
          </select></div>
        <div class="field"><label>发情强度</label>
          <select class="input" v-model.number="f.score">
            <option :value="null">未评分</option><option :value="1">★</option><option :value="2">★★</option>
            <option :value="3">★★★</option><option :value="4">★★★★</option><option :value="5">★★★★★</option>
          </select></div>
      </div>
      <div class="field"><label><input type="checkbox" v-model="f.inseminated"> 本次发情已配种</label></div>
      <template v-if="f.inseminated">
        <div class="field-row">
          <div class="field"><label>配种日期</label>
            <input type="date" class="input" v-model="f.insemination_date" :min="f.date"></div>
          <div class="field"><label>冻精编号 / 公牛号</label><input class="input" v-model="f.semen"></div>
        </div>
        <div class="field-row">
          <div class="field"><label>配种员</label><input class="input" v-model="f.technician"></div>
          <div class="field"><label>孕检结果</label>
            <select class="input" v-model="f.result">
              <option value="pending">待孕检（35~42天后）</option>
              <option value="pregnant">已确认妊娠</option>
              <option value="negative">未孕</option>
              <option value="unknown">未确认</option>
            </select></div>
        </div>
      </template>
      <div class="field"><label>备注</label><textarea class="input" rows="2" v-model="f.note"></textarea></div>
    </div>
    <div class="modal-foot">
      <button class="btn" @click="closeModal">取消</button>
      <button class="btn btn-primary" @click="save">保存</button>
    </div>
  </div></div>`,
};

/* ---------------- 弹窗：牛只详情 ---------------- */
const CowDetailModal = {
  components: { Sparkline },
  setup() {
    const m = topModal();
    const d = ref(null);
    const tab = ref("overview");
    onMounted(async () => {
      try { d.value = await api(`/api/cows/${m.id}`); } catch (e) { toast(e.message, "error"); closeModal(); }
    });
    // 从详情中打开的子弹窗关闭后，自动重新拉取详情
    watch(
      () => S.modals.length,
      async (n, old) => {
        if (n < old && n > 0 && topModal()?.type === "cowDetail" && topModal()?.id === m.id) {
          d.value = await api(`/api/cows/${m.id}`);
        }
      }
    );
    const spark = computed(() =>
      (d.value?.yield_trend || []).map((t) => ({ label: t.date.slice(8), v: t.yield_kg }))
    );
    function addRecord(type) {
      const preset = { presetCow: d.value.id };
      openModal({ type, rec: preset });
    }
    return { d, tab, spark, addRecord, closeModal, SESSION, H_TYPE, SEVERITY, H_RESULT, DETECTION, INSEM_RESULT, addDays };
  },
  template: `
  <div class="modal-mask" @click.self="closeModal"><div class="modal wide" v-if="d">
    <div class="modal-head"><h3>牛只档案详情</h3><button class="modal-close" @click="closeModal">×</button></div>
    <div class="modal-body">
      <div class="detail-head">
        <div class="cow-avatar">🐄</div>
        <div style="flex:1">
          <h2>{{ d.ear_tag }} {{ d.name ? '（' + d.name + '）' : '' }}</h2>
          <div class="detail-meta">
            <span class="badge green">{{ d.status_label }}</span>
            <span class="badge gray">{{ d.breed }} · {{ d.parity }}胎</span>
            <span class="badge gray" v-if="d.group">{{ d.group }}</span>
            <span class="badge blue" v-if="d.days_in_milk != null">泌乳 {{ d.days_in_milk }} 天</span>
            <span class="badge red" v-if="d.in_withdrawal">🚫 休药期至 {{ d.withdrawal.withdrawal_end }}</span>
          </div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-sm" @click="addRecord('milkingForm')">＋挤奶</button>
          <button class="btn btn-sm" @click="addRecord('healthForm')">＋健康</button>
          <button class="btn btn-sm" @click="addRecord('medForm')">＋用药</button>
          <button class="btn btn-sm" @click="addRecord('estrusForm')">＋发情</button>
        </div>
      </div>

      <div class="alert-box danger" v-if="d.in_withdrawal">🚫 {{ d.withdrawal.message }}</div>

      <div class="kv-grid">
        <div><div class="k">出生日期</div><div class="v">{{ d.birth_date }}</div></div>
        <div><div class="k">最近产犊</div><div class="v">{{ d.calving_date || '-' }}</div></div>
        <div><div class="k">预产期</div><div class="v">{{ d.expected_calving_date || '-' }}</div></div>
        <div><div class="k">标定日产</div><div class="v">{{ d.avg_yield_kg ?? '-' }} kg</div></div>
      </div>

      <div class="tabs">
        <button class="tab" :class="{active:tab==='overview'}" @click="tab='overview'">概览</button>
        <button class="tab" :class="{active:tab==='milkings'}" @click="tab='milkings'">挤奶记录</button>
        <button class="tab" :class="{active:tab==='health'}" @click="tab='health'">健康</button>
        <button class="tab" :class="{active:tab==='meds'}" @click="tab='meds'">用药/休药</button>
        <button class="tab" :class="{active:tab==='estruses'}" @click="tab='estruses'">发情配种</button>
      </div>

      <div v-if="tab==='overview'">
        <div class="card-title">近7天日产奶量（不含废弃）</div>
        <sparkline :points="spark"></sparkline>
        <p v-if="d.note" style="color:#6b7280;margin-top:14px">备注：{{ d.note }}</p>
      </div>

      <div v-if="tab==='milkings'" class="table-wrap"><table class="data">
        <thead><tr><th>日期</th><th>班次</th><th class="num">kg</th><th class="num">SCC</th><th>处置</th></tr></thead>
        <tbody>
          <tr v-for="r in d.milkings" :key="r.id">
            <td>{{ r.date }}</td><td>{{ SESSION[r.session] }}</td>
            <td class="num">{{ r.yield_kg }}</td>
            <td class="num"><span :class="r.scc>=500000?'badge red':''">{{ r.scc ?? '-' }}</span></td>
            <td>
              <span v-if="r.violation" class="badge red">违规混装</span>
              <span v-else-if="r.discarded" class="badge amber">废弃</span>
              <span v-else class="badge green">上市</span>
            </td>
          </tr>
        </tbody></table>
      </div>

      <div v-if="tab==='health'" class="table-wrap"><table class="data">
        <thead><tr><th>日期</th><th>类型</th><th>诊断</th><th>体温</th><th>复查</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="h in d.health" :key="h.id">
            <td>{{ h.date }}</td><td>{{ H_TYPE[h.record_type] }}</td>
            <td>{{ h.diagnosis || '-' }}</td>
            <td>{{ h.temperature ? h.temperature+'℃' : '-' }}</td>
            <td>{{ h.follow_up_date || '-' }}</td>
            <td>{{ H_RESULT[h.result] || '未结案' }}</td>
          </tr>
          <tr v-if="!d.health.length"><td colspan="6" class="empty">无记录</td></tr>
        </tbody></table>
      </div>

      <div v-if="tab==='meds'" class="table-wrap"><table class="data">
        <thead><tr><th>日期</th><th>药品</th><th>剂量/途径</th><th>休药期</th><th>可售日</th><th>状态</th></tr></thead>
        <tbody>
          <tr v-for="m in d.medications" :key="m.id">
            <td>{{ m.date }}</td><td>{{ m.drug_name }}</td>
            <td>{{ m.dose || '-' }} {{ m.route ? '· '+m.route : '' }}</td>
            <td>{{ m.withdrawal_days }} 天</td>
            <td><b>{{ addDays(m.withdrawal_end, 1) }}</b></td>
            <td><span v-if="m.active_withdrawal" class="badge red">休药中</span><span v-else class="badge green">已解除</span></td>
          </tr>
          <tr v-if="!d.medications.length"><td colspan="6" class="empty">无用药记录</td></tr>
        </tbody></table>
      </div>

      <div v-if="tab==='estruses'" class="table-wrap"><table class="data">
        <thead><tr><th>发情日</th><th>方式</th><th>配种日</th><th>冻精</th><th>结果</th></tr></thead>
        <tbody>
          <tr v-for="e in d.estruses" :key="e.id">
            <td>{{ e.date }}</td><td>{{ DETECTION[e.detection] }} {{ e.score ? '★'.repeat(e.score) : '' }}</td>
            <td>{{ e.insemination_date || '未配' }}</td><td>{{ e.semen || '-' }}</td>
            <td><span class="badge" :class="{'green':e.result==='pregnant','red':!e.inseminated,'gray':e.result==='negative'}">{{ INSEM_RESULT[e.result] || (e.inseminated ? '待检' : '待配') }}</span></td>
          </tr>
          <tr v-if="!d.estruses.length"><td colspan="5" class="empty">无记录</td></tr>
        </tbody></table>
      </div>
    </div>
  </div></div>`,
};

/* ---------------- 根组件 ---------------- */
const App = {
  components: { Dashboard, CowsPage, MilkingsPage, HealthPage, ReproPage,
    CowFormModal, MilkingFormModal, HealthFormModal, DrugFormModal,
    MedFormModal, EstrusFormModal, CowDetailModal },
  setup() {
    onMounted(async () => {
      try {
        await Promise.all([
          loadDashboard(),
          loadReminders(),
          loadAnomalies(),
          loadCows(),
          loadDrugs(),
        ]);
      } catch (e) { toast(e.message, "error"); }
    });
    const nav = [
      { key: "dashboard", ico: "📊", label: "工作台" },
      { key: "cows", ico: "🐄", label: "奶牛档案" },
      { key: "milkings", ico: "🥛", label: "挤奶记录" },
      { key: "health", ico: "🏥", label: "健康与用药" },
      { key: "repro", ico: "💕", label: "发情与配种" },
    ];
    return { S, switchView, nav, topModal };
  },
  template: `
  <div class="layout">
    <aside class="sidebar">
      <div class="brand"><span class="logo">🐮</span><div>智慧牧场<small>Dairy Farm MS</small></div></div>
      <nav class="nav">
        <button v-for="n in nav" :key="n.key" class="nav-item"
                :class="{active:S.view===n.key}" @click="switchView(n.key)">
          <span class="ico">{{ n.ico }}</span>{{ n.label }}
          <span v-if="n.key==='dashboard' && S.dashboard && S.dashboard.reminder_count"
                class="nav-badge">{{ S.dashboard.reminder_count }}</span>
        </button>
      </nav>
      <div class="sidebar-foot">Vue 3 · FastAPI · SQLite<br>内置 12 头样例牛群数据</div>
    </aside>
    <main class="main">
      <div class="topbar">
        <h1>{{ nav.find(n=>n.key===S.view)?.label }}</h1>
        <div class="date">📅 {{ S.dashboard?.today || '' }} · 牧场管理系统</div>
      </div>
      <div class="content">
        <dashboard v-if="S.view==='dashboard'"></dashboard>
        <cows-page v-else-if="S.view==='cows'"></cows-page>
        <milkings-page v-else-if="S.view==='milkings'"></milkings-page>
        <health-page v-else-if="S.view==='health'"></health-page>
        <repro-page v-else-if="S.view==='repro'"></repro-page>
      </div>
    </main>

    <!-- 弹窗栈 -->
    <template v-for="(md,i) in S.modals" :key="i">
      <cow-form-modal v-if="md.type==='cowForm'"></cow-form-modal>
      <milking-form-modal v-else-if="md.type==='milkingForm'"></milking-form-modal>
      <health-form-modal v-else-if="md.type==='healthForm'"></health-form-modal>
      <drug-form-modal v-else-if="md.type==='drugForm'"></drug-form-modal>
      <med-form-modal v-else-if="md.type==='medForm'"></med-form-modal>
      <estrus-form-modal v-else-if="md.type==='estrusForm'"></estrus-form-modal>
      <cow-detail-modal v-else-if="md.type==='cowDetail'"></cow-detail-modal>
    </template>

    <div class="toast-wrap">
      <div v-for="t in S.toasts" :key="t.id" class="toast" :class="t.type">{{ t.message }}</div>
    </div>
  </div>`,
};

createApp(App).mount("#app");
