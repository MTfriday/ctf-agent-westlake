"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  explainRun,
  getSandbox,
  getSandboxOps,
  SandboxOp,
  SandboxSnapshot,
} from "@/lib/useRun_add";

/**
 * 透明化沙箱面板（0x01）——网页端直接观察容器每一步操作 + 文件结构。
 *
 * 数据源：
 *   · 操作流  GET /api/runs/{id}/sandbox/ops   （每条命令 + 退出码 + 输出摘要）
 *   · 文件树  GET /api/runs/{id}/sandbox       （find 快照 + 存活容器）
 *
 * 运行中自动轮询（操作流 3s / 文件树 8s），结束后保留最后一次快照供回看。
 */

const TREE_REFRESH_MS = 8000;
const OPS_REFRESH_MS = 3000;

function fmtTs(ts: number): string {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false });
}

function fmtBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)}M`;
  if (n >= 1024) return `${(n / 1024).toFixed(1)}K`;
  return `${n}B`;
}

/** 按 path 缩进渲染文件树（扁平行 → 深度前缀）。 */
function TreeView({ rows }: { rows: { type: string; size: number; path: string }[] }) {
  if (!rows.length) {
    return <div className="sb-empty">（暂无文件快照——容器未启动或已被清理）</div>;
  }
  return (
    <div className="sb-tree">
      {rows.map((r, i) => {
        const depth = r.path.split("/").filter(Boolean).length;
        const isDir = r.type === "d";
        return (
          <div
            key={`${r.path}-${i}`}
            className={`sb-tree-row ${isDir ? "dir" : ""}`}
            style={{ paddingLeft: 6 + depth * 14 }}
            title={r.path}
          >
            <span className="sb-tree-icon">{isDir ? "📁" : "📄"}</span>
            <span className="sb-tree-path">{r.path || "/"}</span>
            {!isDir && <span className="sb-tree-size">{fmtBytes(r.size)}</span>}
          </div>
        );
      })}
    </div>
  );
}

export function SandboxPanel({
  runId,
  active,
  open,
  onToggle,
}: {
  runId: string;
  active: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const [ops, setOps] = useState<SandboxOp[]>([]);
  const [snap, setSnap] = useState<SandboxSnapshot | null>(null);
  const [tab, setTab] = useState<"ops" | "tree" | "explain">("ops");
  const [lastFetch, setLastFetch] = useState(0);
  // AI 解说员（0x05）：把黑板 + 操作流翻译成通俗中文解释
  const [explain, setExplain] = useState("");
  const [explaining, setExplaining] = useState(false);
  const [explainErr, setExplainErr] = useState("");
  const bodyRef = useRef<HTMLDivElement>(null);

  const refreshOps = useCallback(async () => {
    if (!runId) return;
    const list = await getSandboxOps(runId, 400);
    setOps(list);
    setLastFetch(Date.now());
  }, [runId]);

  const refreshAll = useCallback(async () => {
    if (!runId) return;
    const [op, sn] = await Promise.all([getSandboxOps(runId, 400), getSandbox(runId)]);
    setOps(op);
    setSnap(sn);
    setLastFetch(Date.now());
  }, [runId]);

  // AI 解说员开关：生成中可「停止」（AbortController 中止 SSE，保留已生成文本）
  const abortRef = useRef<AbortController | null>(null);

  const onExplain = useCallback(async () => {
    if (!runId || explaining) return;
    const ac = new AbortController();
    abortRef.current = ac;
    setExplaining(true);
    setExplain("");
    setExplainErr("");
    const r = await explainRun(runId, (d) => setExplain((prev) => prev + d), ac.signal);
    if (ac.signal.aborted) {
      setExplain((prev) => prev + "\n\n（已停止）");
    } else if (!r.ok) {
      setExplainErr(r.error || "解说失败");
    }
    abortRef.current = null;
    setExplaining(false);
  }, [runId, explaining]);

  const onStopExplain = useCallback(() => {
    abortRef.current?.abort();
  }, []);

  useEffect(() => {
    if (!open || !active) return;
    refreshAll();
    const opsTimer = window.setInterval(refreshOps, OPS_REFRESH_MS);
    const treeTimer = window.setInterval(refreshAll, TREE_REFRESH_MS);
    return () => {
      window.clearInterval(opsTimer);
      window.clearInterval(treeTimer);
    };
  }, [open, active, runId, refreshAll, refreshOps]);

  // 操作流自动滚到底部
  useEffect(() => {
    if (tab !== "ops") return;
    const el = bodyRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [ops, tab]);

  const nContainers = snap?.containers?.length ?? 0;
  const containerLine = snap && snap.containers.length > 0 ? (
    <div className="sb-containers">
      {snap.containers.map((c) => (
        <span key={c.container_id} className="sb-container">
          <span className="sb-container-id">{c.container_id.slice(0, 12)}</span>
          <span className="sb-container-model">{c.model_spec.split("/").pop()}</span>
          <span className="sb-container-ops">{c.ops_count} ops</span>
        </span>
      ))}
    </div>
  ) : null;

  return (
    <aside className={`sandbox-panel ${open ? "open" : "closed"}`} aria-label="透明化沙箱">
      <div className="sb-head">
        <button className="sb-toggle" onClick={onToggle} title={open ? "收起沙箱面板" : "展开沙箱面板"}>
          {open ? "▸" : "◂"}
        </button>
        <span className="sb-title">透明化沙箱</span>
        {open && (
          <>
            <div className="sb-tabs">
              <button className={`sb-tab ${tab === "ops" ? "on" : ""}`} onClick={() => setTab("ops")}>
                操作流{ops.length ? `(${ops.length})` : ""}
              </button>
              <button className={`sb-tab ${tab === "tree" ? "on" : ""}`} onClick={() => setTab("tree")}>
                文件结构
              </button>
              <button className={`sb-tab ${tab === "explain" ? "on" : ""}`} onClick={() => setTab("explain")}>
                解读
              </button>
            </div>
            <button className="sb-refresh" onClick={refreshAll} title="刷新">
              刷新
            </button>
          </>
        )}
      </div>

      {open && (
        <div className="sb-body" ref={bodyRef}>
          {containerLine}
          <div className="sb-meta">
            {nContainers > 0
              ? `容器 ${nContainers} 个 · 上次刷新 ${new Date(lastFetch).toLocaleTimeString("zh-CN", { hour12: false })}`
              : "（无存活容器——等待求解器启动或已结束）"}
          </div>

          {tab === "ops" && (
            <div className="sb-ops">
              {ops.length === 0 && <div className="sb-empty">（暂无操作记录）</div>}
              {ops.map((op, i) => (
                <div className="sb-op" key={`${op.ts}-${i}`}>
                  <div className="sb-op-line">
                    <span className="sb-op-ts">{fmtTs(op.ts)}</span>
                    <span className={`sb-op-exit ${op.exit_code === 0 ? "ok" : "fail"}`}>
                      {op.exit_code === 0 ? "✓" : `✗${op.exit_code}`}
                    </span>
                    <span className="sb-op-model">{op.model_spec.split("/").pop()}</span>
                    <code className="sb-op-cmd">{op.command}</code>
                  </div>
                  {(op.stdout_head || op.stderr_head) && (
                    <details className="sb-op-out">
                      <summary>输出（{op.duration_s.toFixed(1)}s）</summary>
                      <pre className="sb-op-pre">
                        {op.stdout_head}
                        {op.stderr_head ? `\n[stderr]\n${op.stderr_head}` : ""}
                      </pre>
                    </details>
                  )}
                </div>
              ))}
            </div>
          )}

          {tab === "tree" && snap && <TreeView rows={snap.tree} />}

          {tab === "explain" && (
            <div className="sb-explain">
              <div className="sb-explain-bar">
                {explaining ? (
                  <button className="sb-btn danger" onClick={onStopExplain}>
                    停止
                  </button>
                ) : (
                  <button className="sb-btn" onClick={onExplain}>
                    {explain ? "重新解读" : "AI 解读"}
                  </button>
                )}
                <span className="sb-explain-hint">
                  把黑板 + 最近操作翻译成通俗中文（走网关 LLM）
                </span>
              </div>
              {explainErr && <div className="sb-empty fail">{explainErr}</div>}
              {explain && <pre className="sb-explain-pre">{explain}</pre>}
              {!explain && !explaining && !explainErr && (
                <div className="sb-empty">（点击「AI 解读」生成当前进度解释）</div>
              )}
            </div>
          )}
        </div>
      )}
    </aside>
  );
}
