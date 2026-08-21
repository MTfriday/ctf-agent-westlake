"use client";

/**
 * Aemeath「Agent / LLM 配置」面板。
 *
 * Aemeath 使用自建 agent，直接接入 OpenAI 兼容的 LLM 平台（阿里百炼等）。
 *   · LLM 平台 —— 支持多个端点（provider），每个可配 base_url + 规划/路由模型；
 *                把某个设为「当前使用」即全局生效（API key 走 .env，面板不收 key）。
 *   · Agent 编排 —— 引擎后端 + 并发 + 竞速侦察 + 预算；字段带悬停详细说明。
 *
 * 契约：GET/PUT /api/settings/workers → { config: AemeathConfig }。
 */

import { useEffect, useRef, useState } from "react";
import {
  AemeathConfig,
  AemeathLlmProvider,
  getAemeathConfig,
  getRate,
  putAemeathConfig,
  testLlmEndpoint,
} from "@/lib/useRun";
import { useT } from "@/lib/i18n";
import { Icon } from "@/components/Icon";
import { ModelsPanelSection } from "@/components/ModelsPanel";

type Tab = "llm" | "agent" | "models";

const BACKENDS = ["swarm", "hybrid", "orchestrated"] as const;

// ── 引擎后端详细说明（select 下方 + 悬停提示）────────────────────────────
const ENGINE_DESC: Record<string, { name: string; detail: string; tip: string }> = {
  swarm: {
    name: "swarm · 并行竞速",
    detail:
      "多个自建 agent 并行独立求解，各自推理、共享同一张黑板；最快得出 flag 者胜出。适合题目描述明确、需要广撒网快速试探的场景。",
    tip: "所有 agent 同时开工、各自为战，通过共享黑板协同。延迟最低、覆盖面最广，但对题目的理解深度可能不足。",
  },
  hybrid: {
    name: "hybrid · 规划 + 竞速混合",
    detail:
      "先由协调器（planner）做一轮规划：分析附件/靶机、生成最多 4 个意图（Intent），再派发 worker 按意图并行求解；意图的认领与回写驱动后续迭代。默认推荐。",
    tip: "折中方案：规划阶段减少盲目探索，竞速阶段保持并行。默认推荐。",
  },
  orchestrated: {
    name: "orchestrated · 全总控（OODA）",
    detail:
      "完整 OODA 循环：观察黑板 → 理由（LLM 规划意图）→ 决策（认领/派发）→ 行动；循环多轮直到解出或达到轮次上限。推理最深，但延迟最高。",
    tip: "总控主导一切，agent 只是执行意图。适合复杂题目需要分阶段推理，但延迟最高。",
  },
};

// 编排字段悬停说明
const AGENT_TIPS: Record<string, { label: string; tip: string }> = {
  engine_backend: {
    label: "引擎后端",
    tip: "求解引擎如何调度自建 agent，决定「规划深度 vs 并发速度」的取舍。",
  },
  worker_count: {
    label: "并发 Worker 数",
    tip: "同时并发运行的 agent 数量。调高加快并行，但增加 token / 成本消耗；共享黑板会自动去重避免重复劳动。",
  },
  race_scout: {
    label: "竞速侦察",
    tip: "求解开始前先派一批 agent 做快速竞速侦察（单轮、并发），抢先把明显线索 / flag 形式摸清，再进入正式规划循环。适合竞速（CTF）场景。",
  },
  race_timeout: {
    label: "竞速超时（秒）",
    tip: "竞速侦察阶段的最长耗时（秒）；超时自动收敛进入正式规划 / 派发循环。",
  },
  wall_clock_budget: {
    label: "最长运行时限（秒）",
    tip: "整个 run 的最大运行时长（秒），到点自动停止；0 表示不限制。避免求解无限跑下去。",
  },
  cost_budget_usd: {
    label: "成本预算",
    tip: "整个 run 的累计成本上限，人民币（¥）为主、美元（$）自动换算同步。汇率在线获取（后端 /api/settings/rate），点汇率可刷新；0 表示不限制，达到后自动收敛防止预算失控。",
  },
};

/** "?" 帮助图标：悬停 / 聚焦显示详细说明。用 fixed 定位跟随视口，避免被
 *  设置面板的 overflow 裁剪导致显示不全。 */
function Help({ tip }: { tip: React.ReactNode }) {
  const ref = useRef<HTMLSpanElement | null>(null);
  const [pos, setPos] = useState<{ x: number; y: number; below: boolean } | null>(null);
  const show = () => {
    const el = ref.current;
    if (!el) return;
    const r = el.getBoundingClientRect();
    const W = 280;
    let x = r.left + r.width / 2;
    // 视口内钳制，避免贴近边缘时溢出
    x = Math.max(W / 2 + 4, Math.min(x, window.innerWidth - W / 2 - 4));
    // 预估 tooltip 高度；图标下方空间足够则向下展开，否则向上
    const estH = 140;
    const below = r.bottom + 12 + estH <= window.innerHeight;
    const y = below ? r.bottom + 12 : r.top - 10;
    setPos({ x, y, below });
  };
  const hide = () => setPos(null);
  return (
    <span
      ref={ref}
      className="ws2-help"
      tabIndex={0}
      role="button"
      aria-label="帮助"
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
    >
      ?
      {pos && (
        <span
          className={`ws2-tip ws2-tip-fixed${pos.below ? " below" : ""}`}
          style={{ left: pos.x, top: pos.y, bottom: "auto", right: "auto" }}
        >
          {tip}
        </span>
      )}
    </span>
  );
}

export function WorkerSettings({
  open,
  onClose,
  initialTab,
}: {
  open: boolean;
  onClose: () => void;
  initialTab?: Tab;
}) {
  const t = useT();
  const [tab, setTab] = useState<Tab>(initialTab ?? "llm");
  const [draft, setDraft] = useState<AemeathConfig | null>(null);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [testRes, setTestRes] = useState<{ id: string; ok: boolean; detail: string; model?: string } | null>(null);
  const [savedMsg, setSavedMsg] = useState<string | null>(null);
  // 成本预算双币种（CNY/USD 联动）+ 在线汇率
  const [rate, setRate] = useState<number>(7.2);
  const [rateSrc, setRateSrc] = useState<string>("");
  const [costCny, setCostCny] = useState<number>(0);
  const [costUsd, setCostUsd] = useState<number>(0);
  const modalRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    (async () => {
      const [c, r] = await Promise.all([getAemeathConfig(), getRate()]);
      if (!alive) return;
      if (r && r.usd_cny > 0) {
        setRate(r.usd_cny);
        setRateSrc(r.source);
      }
      setDraft(c);
      const usd = c?.agent.cost_budget_usd ?? 0;
      setCostUsd(usd);
      setCostCny(Math.round(usd * (r?.usd_cny ?? 7.2)));
      setDirty(false);
      setSavedMsg(null);
      setTestRes(null);
    })();
    return () => { alive = false; };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open || !draft) return null;
  const d = draft;
  const mark = () => setDirty(true);

  // ── LLM providers ────────────────────────────────────────────────────
  const setActive = (id: string) => {
    setDraft({ ...d, llm: { ...d.llm, active: id } });
    mark();
  };
  const setProvider = (i: number, patch: Partial<AemeathLlmProvider>) => {
    const providers = d.llm.providers.map((p, idx) => (idx === i ? { ...p, ...patch } : p));
    setDraft({ ...d, llm: { ...d.llm, providers } });
    mark();
  };
  const addProvider = () => {
    const n = d.llm.providers.length + 1;
    const provider: AemeathLlmProvider = {
      id: `custom-${Date.now().toString(36)}`,
      label: `自定义端点 ${n}`,
      base_url: "",
      api_key_present: false,
      model_planner: "",
      model_router: "",
      enabled: true,
    };
    setDraft({ ...d, llm: { ...d.llm, providers: [...d.llm.providers, provider] } });
    mark();
  };
  const removeProvider = (i: number) => {
    const providers = d.llm.providers.filter((_, idx) => idx !== i);
    let active = d.llm.active;
    if (providers.length === 0) return;
    if (!providers.some((p) => p.id === active)) {
      active = providers.find((p) => p.enabled)?.id ?? providers[0].id;
    }
    setDraft({ ...d, llm: { ...d.llm, providers, active } });
    mark();
  };

  // ── agent ────────────────────────────────────────────────────────────
  const setAgent = (patch: Partial<typeof d.agent>) => {
    setDraft({ ...d, agent: { ...d.agent, ...patch } });
    mark();
  };

  const save = async () => {
    setSaving(true);
    const next = await putAemeathConfig({
      llm: { active: d.llm.active, providers: d.llm.providers },
      agent: { ...d.agent, cost_budget_usd: costUsd },
    });
    setSaving(false);
    if (next) {
      setDraft(next);
      setCostUsd(next.agent.cost_budget_usd ?? 0);
      setCostCny(Math.round((next.agent.cost_budget_usd ?? 0) * rate));
      setDirty(false);
      setSavedMsg("已保存");
    } else {
      setSavedMsg("保存失败");
    }
  };

  // ── 成本预算 CNY/USD 双向联动 ──────────────────────────────────────
  const onCnyChange = (v: number) => {
    const c = Math.max(0, v || 0);
    setCostCny(c);
    const usd = Number((c / (rate || 7.2)).toFixed(2));
    setCostUsd(usd);
    setDraft({ ...d, agent: { ...d.agent, cost_budget_usd: usd } });
    mark();
  };
  const onUsdChange = (v: number) => {
    const usd = Math.max(0, v || 0);
    setCostUsd(usd);
    const cny = Math.round(usd * (rate || 7.2));
    setCostCny(cny);
    setDraft({ ...d, agent: { ...d.agent, cost_budget_usd: usd } });
    mark();
  };
  const refreshRate = async () => {
    const r = await getRate();
    if (!r || r.usd_cny <= 0) return;
    setRate(r.usd_cny);
    setRateSrc(r.source);
    const usd = Number((costCny / r.usd_cny).toFixed(2));
    setCostUsd(usd);
    setDraft({ ...d, agent: { ...d.agent, cost_budget_usd: usd } });
    mark();
  };

  const runTest = async (p: AemeathLlmProvider) => {
    setTestingId(p.id);
    setTestRes(null);
    const r = await testLlmEndpoint("planner", p.base_url, p.model_planner || p.model_router || "model");
    setTestingId(null);
    setTestRes({ id: p.id, ...r });
  };

  const eng = ENGINE_DESC[d.agent.engine_backend] ?? ENGINE_DESC.swarm;
  const activeKey = d.llm.providers.find((p) => p.id === d.llm.active);

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal worker-settings ws2"
        ref={modalRef}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label={t("settings.title")}
      >
        <div className="modal-head">
          <div>
            <span>{t("settings.title")}</span>
            <p>Aemeath 自建 agent · 直接接入 LLM 平台（无需 claude/codex CLI）</p>
          </div>
          <button className="modal-x" onClick={onClose} title={t("settings.close")} aria-label={t("settings.close")}>
            <Icon name="x" size={15} />
          </button>
        </div>

        <div className="ws2-health" aria-label="config state">
          <span className={`ws2-draft ${dirty ? "dirty" : "clean"}`}>
            <Icon name={dirty ? "pencil" : "check"} size={11} />
            {dirty ? "有未保存的修改" : "已保存"}
          </span>
          <span className="ws2-health-sep" aria-hidden>|</span>
          <span className="ws2-seg">
            {activeKey ? `当前端点：${activeKey.label || activeKey.id}` : "未启用端点"}
          </span>
        </div>

        <div className="ws2-body">
          <nav className="ws2-rail" aria-label={t("settings.title")}>
            <button type="button" className={`ws2-tab ${tab === "llm" ? "on" : ""}`} onClick={() => setTab("llm")} aria-current={tab === "llm"}>
              <Icon name="gear" size={16} />
              <span>LLM 平台</span>
            </button>
            <button type="button" className={`ws2-tab ${tab === "agent" ? "on" : ""}`} onClick={() => setTab("agent")} aria-current={tab === "agent"}>
              <Icon name="cpu" size={16} />
              <span>Agent 编排</span>
            </button>
            <button type="button" className={`ws2-tab ${tab === "models" ? "on" : ""}`} onClick={() => setTab("models")} aria-current={tab === "models"}>
              <Icon name="layers" size={16} />
              <span>模型池</span>
            </button>
          </nav>

          <div className="ws2-content">
            {tab === "llm" && (
              <section>
                <div className="ws-section-head">
                  <h3>LLM 平台（OpenAI 兼容，支持多端点）</h3>
                  <span>自建 agent 通过「当前使用」的端点完成全部规划 / 路由 / 求解推理。可维护多个端点并随时切换。</span>
                </div>
                <div className="ws-note ws-note-info">
                  API key 请在 .env 配置（BAILIAN_API_KEY / DEEPSEEK_API_KEY 等），面板不收集 key。
                </div>

                {d.llm.providers.map((p, i) => (
                  <div className={`ws2-provider ${p.id === d.llm.active ? "active" : ""}`} key={p.id}>
                    <div className="ws2-provider-head">
                      <input
                        className="ws2-provider-label"
                        value={p.label}
                        placeholder="端点名称"
                        onChange={(e) => setProvider(i, { label: e.target.value })}
                        spellCheck={false}
                      />
                      {p.id === d.llm.active ? (
                        <span className="ws2-provider-active" title="当前使用此端点">
                          <Icon name="check" size={12} /> 当前使用
                        </span>
                      ) : (
                        <button type="button" className="ws2-seg" onClick={() => setActive(p.id)} title="切换到此端点">
                          设为当前
                        </button>
                      )}
                      <button
                        type="button"
                        className="ws-mini-btn danger ws2-provider-del"
                        onClick={() => removeProvider(i)}
                        title="删除此端点"
                        disabled={d.llm.providers.length <= 1}
                      >
                        <Icon name="x" size={12} />
                      </button>
                    </div>

                    <div className="ws-grid">
                      <div className="ws-field ws-span-all">
                        <div className="ws2-field-row">
                          <label>Base URL</label>
                          <Help tip={<>OpenAI 兼容的端点地址。阿里百炼示例：<code>https://dashscope.aliyuncs.com/compatible-mode/v1</code></>} />
                        </div>
                        <input
                          value={p.base_url}
                          placeholder="https://dashscope.aliyuncs.com/compatible-mode/v1"
                          onChange={(e) => setProvider(i, { base_url: e.target.value })}
                          spellCheck={false}
                        />
                      </div>
                      <div className="ws-field">
                        <div className="ws2-field-row">
                          <label>规划模型（planner）</label>
                          <Help tip="协调器大脑：生成求解意图、审计进展。推荐 qwen3.7-max 等强模型。" />
                        </div>
                        <input value={p.model_planner} onChange={(e) => setProvider(i, { model_planner: e.target.value })} spellCheck={false} />
                      </div>
                      <div className="ws-field">
                        <div className="ws2-field-row">
                          <label>路由模型（router）</label>
                          <Help tip="低成本路由模型：快速决策解题模式 / 意图路由。推荐 qwen3.6-flash。" />
                        </div>
                        <input value={p.model_router} onChange={(e) => setProvider(i, { model_router: e.target.value })} spellCheck={false} />
                      </div>
                      <div className="ws-field ws-span-all">
                        <label>API Key</label>
                        <span className={p.api_key_present ? "ws-ok" : "ws-bad"}>
                          <Icon name={p.api_key_present ? "check" : "alert"} size={13} />
                          {p.api_key_present ? `已配置（${p.id} 的环境变量）` : "未配置 —— 该端点求解将不可用"}
                        </span>
                      </div>
                    </div>

                    <div className="ws-foot" style={{ justifyContent: "flex-start" }}>
                      <button className="ws-mini-btn" type="button" onClick={() => runTest(p)} disabled={testingId === p.id}>
                        <Icon name="plug" size={13} />
                        {testingId === p.id ? "测试中…" : "测试连接"}
                      </button>
                      {testRes && testRes.id === p.id && (
                        <span className={testRes.ok ? "ws-ok" : "ws-bad"} title={testRes.detail}>
                          <Icon name={testRes.ok ? "check" : "x"} size={13} />
                          {testRes.ok ? `连接成功（${testRes.model}）` : (testRes.detail || "失败").slice(0, 80)}
                        </span>
                      )}
                    </div>
                  </div>
                ))}

                <button className="ws-mini-btn" type="button" onClick={addProvider}>
                  ＋ 添加端点
                </button>
              </section>
            )}

            {tab === "models" && (
              <section>
                <div className="ws-section-head">
                  <h3>求解器模型池</h3>
                  <span>
                    config.yaml solver.models 为单一数据源（与默认 Worker 配置统一）。
                    配额耗尽自动剔除（403）；可实时添加/删除，运行中的 lane 热插拔生效。
                  </span>
                </div>
                <ModelsPanelSection />
              </section>
            )}
            {tab === "agent" && (
              <section>
                <div className="ws-section-head">
                  <h3>Agent 编排</h3>
                  <span>求解引擎如何调度自建 agent（决定规划深度、并发与预算）。把鼠标悬停在 ? 上看详细说明。</span>
                </div>
                <div className="ws-grid">
                  <div className="ws-field">
                    <div className="ws2-field-row">
                      <label>{AGENT_TIPS.engine_backend.label}</label>
                      <Help tip={AGENT_TIPS.engine_backend.tip} />
                    </div>
                    <select value={d.agent.engine_backend} onChange={(e) => setAgent({ engine_backend: e.target.value })}>
                      {BACKENDS.map((b) => <option value={b} key={b}>{ENGINE_DESC[b]?.name ?? b}</option>)}
                    </select>
                  </div>
                  <div className="ws-field">
                    <div className="ws2-field-row">
                      <label>{AGENT_TIPS.worker_count.label}</label>
                      <Help tip={AGENT_TIPS.worker_count.tip} />
                    </div>
                    <input
                      type="number" min={1} value={d.agent.worker_count}
                      onChange={(e) => setAgent({ worker_count: Math.max(1, parseInt(e.target.value) || 1) })}
                    />
                  </div>
                  <div className="ws-field">
                    <div className="ws2-field-row">
                      <label>{AGENT_TIPS.race_scout.label}</label>
                      <Help tip={AGENT_TIPS.race_scout.tip} />
                    </div>
                    <select value={d.agent.race_scout ? "1" : "0"} onChange={(e) => setAgent({ race_scout: e.target.value === "1" })}>
                      <option value="1">启用</option>
                      <option value="0">停用</option>
                    </select>
                  </div>
                  <div className="ws-field">
                    <div className="ws2-field-row">
                      <label>{AGENT_TIPS.race_timeout.label}</label>
                      <Help tip={AGENT_TIPS.race_timeout.tip} />
                    </div>
                    <input
                      type="number" min={0} value={d.agent.race_timeout}
                      onChange={(e) => setAgent({ race_timeout: Math.max(0, parseInt(e.target.value) || 0) })}
                    />
                  </div>
                  <div className="ws-field">
                    <div className="ws2-field-row">
                      <label>{AGENT_TIPS.wall_clock_budget.label}</label>
                      <Help tip={AGENT_TIPS.wall_clock_budget.tip} />
                    </div>
                    <input
                      type="number" min={0} value={d.agent.wall_clock_budget}
                      onChange={(e) => setAgent({ wall_clock_budget: Math.max(0, parseInt(e.target.value) || 0) })}
                    />
                  </div>
                  <div className="ws-field ws-span-all">
                    <div className="ws2-field-row">
                      <label>{AGENT_TIPS.cost_budget_usd.label}</label>
                      <Help tip={AGENT_TIPS.cost_budget_usd.tip} />
                    </div>
                    <div className="ws2-cost-grid">
                      <div className="ws2-cost-input">
                        <span className="ws2-cost-cur">¥</span>
                        <input
                          type="number" min={0} step="0.01" value={costCny}
                          onChange={(e) => onCnyChange(parseFloat(e.target.value) || 0)}
                          placeholder="0"
                        />
                      </div>
                      <div className="ws2-cost-input">
                        <span className="ws2-cost-cur">$</span>
                        <input
                          type="number" min={0} step="0.01" value={costUsd}
                          onChange={(e) => onUsdChange(parseFloat(e.target.value) || 0)}
                          placeholder="0"
                        />
                      </div>
                      <button type="button" className="ws2-rate-hint" onClick={refreshRate} title="点击刷新汇率">
                        ↻ 1 USD = {rate.toFixed(4)} CNY{rateSrc ? ` · ${rateSrc}` : ""}
                      </button>
                    </div>
                  </div>
                </div>

                <div className="ws2-backend-desc">
                  <b>{eng.name}</b> —— {eng.detail}
                </div>
              </section>
            )}
          </div>
        </div>

        <div className="ws-foot">
          <span className="ws2-foot-note">
            {savedMsg ?? (dirty ? "有未保存的修改" : "已保存")}
          </span>
          <span className="ws2-foot-actions">
            <button className="ws2-cancel" type="button" onClick={onClose}>取消</button>
            <button className="ws2-save" type="button" onClick={save} disabled={saving || !dirty}>
              {saving ? "保存中…" : "保存"}
            </button>
          </span>
        </div>
      </div>
    </div>
  );
}
