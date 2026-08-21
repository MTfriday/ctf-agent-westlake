"use client";

import { useCallback, useEffect, useState } from "react";
import { getWorkerModelOptions } from "@/lib/useRun";
import {
  addWorkerModel,
  getModelHealth,
  removeWorkerModel,
  resetModel,
  testModel,
} from "@/lib/useRun_add";

/**
 * 可用 AI 模型（求解器模型池）——0x04/0x05。
 *
 * ModelsPanelSection：无外壳内容组件，嵌入「默认 Worker 配置」的「模型池」
 * tab（0x06 面板融合，单一数据源 = config.yaml solver.models）。
 * ModelsPanel：独立弹窗外壳（保留原入口兼容）。
 * 数据源：
 *   · GET /api/settings/worker-models         模型池（aemeath 组）
 *   · GET /api/settings/worker-model/health   每模型健康（禁用/失败数/冷却/最近错误）
 *   · POST /api/settings/worker-model/test    真实探测（发一条 ping）
 *   · POST /api/settings/worker-model/{id}/reset  手动恢复
 */

interface ModelRow {
  id: string;
  label: string;
  disabled?: boolean;
  health?: Record<string, any>;
}

const short = (id: string) => (id.includes("/") ? id.split("/").pop() : id);

export function ModelsPanelSection() {
  const [rows, setRows] = useState<ModelRow[]>([]);
  const [testing, setTesting] = useState<string>("");
  const [results, setResults] = useState<Record<string, { ok: boolean; detail: string }>>({});
  const [err, setErr] = useState("");
  // 模型池管理（0x05）：前端添加/删除模型（config.yaml 持久化 + 热插拔 lane）
  const [addSpec, setAddSpec] = useState("");
  const [adding, setAdding] = useState(false);
  const [removing, setRemoving] = useState<string>("");
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const load = useCallback(async () => {
    try {
      const [opts, h] = await Promise.all([getWorkerModelOptions(), getModelHealth()]);
      const list: ModelRow[] = [];
      for (const engine of Object.keys(opts?.models ?? {})) {
        for (const m of opts.models[engine] ?? []) {
          list.push({
            id: m.id,
            label: m.label || m.id,
            disabled: Boolean(h?.[m.id]?.disabled),
            health: h?.[m.id],
          });
        }
      }
      setRows(list);
      setErr("");
    } catch (e) {
      setErr(String(e));
    }
  }, []);

  useEffect(() => {
    load();
    const timer = window.setInterval(load, 15000);
    return () => window.clearInterval(timer);
  }, [load]);

  const onTest = async (id: string) => {
    setTesting(id);
    const r = await testModel(id);
    setResults((prev) => ({ ...prev, [id]: { ok: r.ok, detail: r.detail || "" } }));
    setTesting("");
  };

  const onReset = async (id: string) => {
    const ok = await resetModel(id);
    if (ok) {
      setResults((prev) => ({ ...prev, [id]: { ok: true, detail: "已恢复" } }));
      load();
    }
  };

  const onAdd = async () => {
    const spec = addSpec.trim();
    if (!spec || adding) return;
    setAdding(true);
    setMsg(null);
    const r = await addWorkerModel(spec);
    if (r.ok) {
      setMsg({ ok: true, text: `已添加 ${spec}` });
      setAddSpec("");
      load();
    } else {
      setMsg({ ok: false, text: r.error || "添加失败" });
    }
    setAdding(false);
  };

  const onRemove = async (id: string) => {
    if (removing) return;
    if (!window.confirm(`确定从模型池删除 ${id}？\n（若正在求解，对应 lane 会被终止）`)) return;
    setRemoving(id);
    setMsg(null);
    const r = await removeWorkerModel(id);
    if (r.ok) {
      setMsg({ ok: true, text: `已删除 ${id}` });
      load();
    } else {
      setMsg({ ok: false, text: r.error || "删除失败" });
    }
    setRemoving("");
  };

  return (
    <>
      <div className="mp-note">
        <span className={rows.some((r) => r.disabled) ? "mp-badge warn" : "mp-badge ok"}>
          {rows.some((r) => r.disabled) ? "有模型被禁用（配额/错误）" : "全部模型可用"}
        </span>
        <button className="mp-reload" onClick={load}>刷新</button>
      </div>

      {err && <div className="mp-err">{err}</div>}

      <div className="mp-add">
        <input
          className="mp-add-input"
          placeholder="添加模型 spec，如 gateway-bailian/qwen3.7-flash"
          value={addSpec}
          onChange={(e) => setAddSpec(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") onAdd();
          }}
        />
        <button className="mp-btn" onClick={onAdd} disabled={adding || !addSpec.trim()}>
          {adding ? "添加中…" : "添加"}
        </button>
      </div>
      {msg && <div className={`mp-msg ${msg.ok ? "ok" : "fail"}`}>{msg.text}</div>}

      <div className="mp-list">
        {rows.length === 0 && <div className="mp-empty">（无可用模型——检查 config.yaml solver.models）</div>}
        {rows.map((r) => {
          const h = r.health || {};
          const res = results[r.id];
          return (
            <div className={`mp-row ${r.disabled ? "disabled" : ""}`} key={r.id}>
              <div className="mp-row-main">
                <span className="mp-model">{short(r.id)}</span>
                <span className={`mp-state ${r.disabled ? "off" : "on"}`}>
                  {r.disabled ? "禁用" : "可用"}
                </span>
                {typeof h.consecutive_errors === "number" && h.consecutive_errors > 0 && (
                  <span className="mp-fail">连续失败 {h.consecutive_errors} 次</span>
                )}
                {typeof h.quota_hits === "number" && h.quota_hits > 0 && (
                  <span className="mp-cool">配额触发 {h.quota_hits} 次</span>
                )}
                {typeof h.cooldown_remaining === "number" && h.cooldown_remaining > 0 && (
                  <span className="mp-cool">冷却 {h.cooldown_remaining}s</span>
                )}
                {res && (
                  <span className={`mp-result ${res.ok ? "ok" : "fail"}`}>
                    {res.ok ? "✓ " : "✗ "}
                    {res.detail}
                  </span>
                )}
              </div>
              {h.last_error && <div className="mp-err-detail" title={h.last_error}>{h.last_error}</div>}
              <div className="mp-row-actions">
                <button
                  className="mp-btn"
                  disabled={!!testing}
                  onClick={() => onTest(r.id)}
                >
                  {testing === r.id ? "测试中…" : "测试"}
                </button>
                {r.disabled && (
                  <button className="mp-btn" onClick={() => onReset(r.id)}>恢复</button>
                )}
                <button
                  className="mp-btn danger"
                  onClick={() => onRemove(r.id)}
                  disabled={!!removing}
                >
                  {removing === r.id ? "删除中…" : "删除"}
                </button>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

export function ModelsPanel({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal worker-settings models-panel"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="可用 AI 模型"
      >
        <div className="modal-head">
          <div>
            <span>可用 AI 模型</span>
            <p>求解器模型池 · 实时健康状态 · 配额耗尽自动剔除（免费额度用完即 403）</p>
          </div>
          <button className="modal-x" onClick={onClose} title="关闭" aria-label="关闭">
            ✕
          </button>
        </div>

        <ModelsPanelSection />
      </div>
    </div>
  );
}
