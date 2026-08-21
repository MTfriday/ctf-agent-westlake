// 网页端重构（0x01/0x03/0x04）：透明化沙箱 / 双模式 / 模型列表的 API 层。
// 独立模块，避免改动 useRun.ts 既有逻辑；组件从 "@/lib/useRun_add" 引入。
import { apiFetch } from "./useRun";

// ── 透明化沙箱（0x01）──────────────────────────────────────────────────────

/** 容器里执行的一条操作：命令 + 退出码 + 输出摘要 + 耗时。 */
export interface SandboxOp {
  ts: number;
  model_spec: string;
  command: string;
  exit_code: number;
  stdout_head?: string;
  stderr_head?: string;
  duration_s: number;
}

export interface SandboxContainer {
  container_id: string;
  image: string;
  workspace_dir: string;
  model_spec: string;
  started_ts: number;
  ops_count: number;
}

export interface SandboxSnapshot {
  run_id: string;
  problem_id: string;
  containers: SandboxContainer[];
  tree: { type: string; size: number; path: string }[];
}

/** 容器的当前状态：存活容器列表 + 文件结构树（find 快照）。失败返回 null。 */
export async function getSandbox(runId: string): Promise<SandboxSnapshot | null> {
  try {
    const r = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/sandbox`);
    if (!r.ok) return null;
    const j = await r.json();
    return {
      run_id: j.run_id ?? runId,
      problem_id: j.problem_id ?? runId,
      containers: j.containers ?? [],
      tree: j.tree ?? [],
    };
  } catch {
    return null;
  }
}

/** 容器操作流（时间升序，最多 limit 条）。失败返回 []。 */
export async function getSandboxOps(runId: string, limit = 300): Promise<SandboxOp[]> {
  try {
    const r = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/sandbox/ops?limit=${limit}`);
    if (!r.ok) return [];
    const j = await r.json();
    return (j.ops ?? []) as SandboxOp[];
  } catch {
    return [];
  }
}

// ── 双模式（0x03）──────────────────────────────────────────────────────────

/** 命名创建 run（解题模式：题目名即 run id）。返回 run_id，失败返回 ""。 */
export async function createRun(name: string): Promise<string> {
  try {
    const r = await apiFetch(`/api/runs`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name }),
    });
    if (!r.ok) return "";
    const j = await r.json();
    return j.run_id ?? "";
  } catch {
    return "";
  }
}

/** 平台题目（比赛模式列表展示用）。字段来自 CTFd/DASCTF detail 结构。 */
export interface PlatformChallenge {
  id?: string;
  name: string;
  category?: string;
  value?: number;
  description?: string;
  connection_info?: string;
  files?: unknown[];
  solved_by_me?: boolean;
}

/** 强制重拉平台题目列表（同步按钮）。失败返回 null。 */
export async function syncPlatform(): Promise<{ ok: boolean; total?: number; added?: string[]; unsolved?: string[] } | null> {
  try {
    const r = await apiFetch(`/api/platform/sync`, { method: "POST" });
    if (!r.ok) return null;
    return (await r.json()) as { ok: boolean; total?: number; added?: string[]; unsolved?: string[] };
  } catch {
    return null;
  }
}

/** 平台题目原始列表（比赛模式面板）。失败返回 []。 */
export async function getPlatformChallenges(): Promise<PlatformChallenge[]> {
  try {
    const r = await apiFetch(`/api/platform/challenges`);
    if (!r.ok) return [];
    const j = await r.json();
    return (j.challenges ?? []) as PlatformChallenge[];
  } catch {
    return [];
  }
}

// ── 可用 AI 模型列表（0x04）────────────────────────────────────────────────

export interface ModelHealth {
  spec?: string;
  disabled?: boolean;
  fatal?: boolean;
  consecutive_errors?: number;
  quota_hits?: number;
  last_error?: string | null;
  cooldown_remaining?: number;
  success_count?: number;
}

/** 每模型的健康状态（禁用 / 失败数 / 冷却 / 最近错误），按 model spec 键。 */
export async function getModelHealth(): Promise<Record<string, ModelHealth>> {
  try {
    const r = await apiFetch(`/api/settings/worker-model/health`);
    if (!r.ok) return {};
    const j = await r.json();
    return (j.models ?? {}) as Record<string, ModelHealth>;
  } catch {
    return {};
  }
}

/** 真实探测：给指定模型发一条 ping，验证配额/鉴权/网络可用。 */
export async function testModel(spec: string): Promise<{ ok: boolean; detail: string; status?: number; model?: string }> {
  try {
    const r = await apiFetch(`/api/settings/worker-model/test`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: spec }),
    });
    const j = await r.json().catch(() => ({}));
    return { ok: Boolean(j?.ok), detail: String(j?.detail ?? (r.ok ? "ok" : `HTTP ${r.status}`)), status: r.status, model: j?.model };
  } catch (e) {
    return { ok: false, detail: String(e) };
  }
}

/** 手动恢复一个被禁用的模型。 */
// ── 模型池管理（0x05）：前端增删模型（config.yaml 持久化 + 热插拔 lane）──

export async function addWorkerModel(spec: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const r = await apiFetch(`/api/settings/worker-models`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ spec }),
    });
    if (!r.ok) {
      const j = await r.json().catch(() => null);
      return { ok: false, error: j?.error || `HTTP ${r.status}` };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

export async function removeWorkerModel(spec: string): Promise<{ ok: boolean; error?: string }> {
  try {
    const r = await apiFetch(`/api/settings/worker-models/${encodeURIComponent(spec)}`, {
      method: "DELETE",
    });
    if (!r.ok) {
      const j = await r.json().catch(() => null);
      return { ok: false, error: j?.error || `HTTP ${r.status}` };
    }
    return { ok: true };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
}

// ── AI 解说员（0x05）：SSE 流式中文解读（黑板 + 沙箱操作流 → 通俗解释）───

export async function explainRun(
  runId: string,
  onDelta: (text: string) => void,
  signal?: AbortSignal,
): Promise<{ ok: boolean; error?: string }> {
  try {
    const r = await apiFetch(`/api/runs/${encodeURIComponent(runId)}/explain`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
      signal,
    });
    if (!r.ok || !r.body) {
      const txt = await r.text().catch(() => "");
      return { ok: false, error: `HTTP ${r.status}: ${txt.slice(0, 160)}` };
    }
    const reader = r.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      // sse-starlette 帧以空行分隔；容忍 CRLF/LF
      const frames = buf.split(/\r?\n\r?\n/);
      buf = frames.pop() || "";
      for (const frame of frames) {
        const m = frame.match(/^data: (.+)$/s);
        if (!m) continue;
        let obj: any;
        try {
          obj = JSON.parse(m[1]);
        } catch {
          continue;
        }
        if (obj.error) return { ok: false, error: String(obj.error) };
        if (obj.delta) onDelta(String(obj.delta));
      }
    }
    return { ok: true };
  } catch (e) {
    if (signal?.aborted) return { ok: true };
    return { ok: false, error: String(e) };
  }
}

export async function resetModel(spec: string): Promise<boolean> {
  try {
    const r = await apiFetch(`/api/settings/worker-model/${encodeURIComponent(spec)}/reset`, { method: "POST" });
    return r.ok;
  } catch {
    return false;
  }
}
