"use client";

import { useCallback, useEffect, useState } from "react";
import { getPlatformChallenges, syncPlatform, PlatformChallenge } from "@/lib/useRun_add";

/**
 * 比赛模式题目列表（0x03）——从 DASCTF 平台拉取题目，选择后自动下载附件、自行解题提交。
 * 数据源：GET /api/platform/challenges（原始列表）+ POST /api/platform/sync（强制重拉）。
 */

export function ChallengesModal({
  open,
  onClose,
  onLaunch,
}: {
  open: boolean;
  onClose: () => void;
  onLaunch: (ch: PlatformChallenge) => Promise<boolean>;
}) {
  const [list, setList] = useState<PlatformChallenge[]>([]);
  const [loading, setLoading] = useState(false);
  const [syncing, setSyncing] = useState(false);
  const [err, setErr] = useState("");
  const [launching, setLaunching] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const l = await getPlatformChallenges();
      setList(l);
      setErr("");
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const onSync = async () => {
    setSyncing(true);
    await syncPlatform();
    await load();
    setSyncing(false);
  };

  if (!open) return null;

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div
        className="modal ch-modal"
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-label="比赛模式 · 平台题目"
      >
        <div className="modal-head">
          <div>
            <span>比赛模式 · 平台题目</span>
            <p>从 DASCTF 平台拉取题目：选择后自动下载附件并自行解题、提交 flag</p>
          </div>
          <button className="modal-x" onClick={onClose} title="关闭" aria-label="关闭">
            ✕
          </button>
        </div>

        <div className="ch-bar">
          <button className="mp-reload" onClick={onSync} disabled={syncing}>
            {syncing ? "同步中…" : "重新同步"}
          </button>
          <span className="ch-count">共 {list.length} 题</span>
        </div>

        {err && <div className="mp-err">{err}</div>}

        <div className="ch-list">
          {loading && <div className="mp-empty">加载中…</div>}
          {!loading && list.length === 0 && <div className="mp-empty">（平台无题目或不可达）</div>}
          {list.map((c) => {
            const solved = Boolean(c.solved_by_me);
            const nFiles = Array.isArray(c.files) ? c.files.length : 0;
            return (
              <div className="ch-row" key={c.id ?? c.name}>
                <div className="ch-main">
                  <span className="ch-name" title={c.name}>{c.name}</span>
                  <span className="ch-cat">{c.category || "?"}</span>
                  {typeof c.value === "number" && c.value > 0 && (
                    <span className="ch-val">{c.value} pts</span>
                  )}
                  {nFiles > 0 && <span className="ch-files">{nFiles} 附件</span>}
                  {solved && <span className="ch-solved">已解出</span>}
                </div>
                <button
                  className="mp-btn"
                  disabled={solved || launching === c.name}
                  onClick={async () => {
                    setLaunching(c.name);
                    const ok = await onLaunch(c);
                    if (ok) setLaunching("");
                  }}
                >
                  {launching === c.name ? "启动中…" : solved ? "已解出" : "开始解题"}
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
