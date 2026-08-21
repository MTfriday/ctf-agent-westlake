"use client";

import { useRef, useState } from "react";
import type { ChangeEvent } from "react";
import { createRun } from "@/lib/useRun_add";
import { uploadFiles } from "@/lib/useRun";
import type { SavedFile } from "@/lib/useRun";

/**
 * 解题模式表单（0x03）——人工填写题目信息 + 上传/粘贴附件 URL 后自行解题。
 *
 * 流程：命名创建 run（题目名即 run_id）→ 附件上传到该 run → POST /start
 * {mode:"manual", no_submit:true, challenge:{name, category, target,
 * description, attachments:[本地路径..., URL...]}} → onStarted(runId)。
 * 后端不访问平台、不提交 flag（no_submit），解出的 flag 只回显网页。
 */

export function ManualLaunchForm({
  onStart,
  onStarted,
  onCancel,
}: {
  onStart: (body: Record<string, unknown>, overrideRunId?: string) => Promise<unknown>;
  onStarted: (runId: string) => void;
  onCancel: () => void;
}) {
  const [name, setName] = useState("");
  const [category, setCategory] = useState("");
  const [target, setTarget] = useState("");
  const [description, setDescription] = useState("");
  const [urls, setUrls] = useState("");
  const [files, setFiles] = useState<SavedFile[]>([]);
  const [runId, setRunId] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);

  // 附件需要挂在真实 run 下：先命名创建（题目名即 run_id），上传后复用同一 run。
  const onPickFiles = async (e: ChangeEvent<HTMLInputElement>) => {
    const picked = e.target.files;
    if (!picked || picked.length === 0) return;
    if (!name.trim()) {
      setErr("请先填写题目名，再上传附件（题目名将作为 run 标识）");
      if (fileRef.current) fileRef.current.value = "";
      return;
    }
    let id = runId;
    if (!id) {
      id = await createRun(name.trim());
      if (id) setRunId(id);
    }
    if (!id) {
      setErr("创建 run 失败（后端不可达？）");
      return;
    }
    const saved = await uploadFiles(id, picked);
    if (saved.length) {
      setFiles((prev) => [...prev, ...saved]);
      setErr("");
    } else {
      setErr("附件上传失败");
    }
    if (fileRef.current) fileRef.current.value = "";
  };

  const removeFile = (path: string) => setFiles((prev) => prev.filter((f) => f.path !== path));

  const onSubmit = async () => {
    if (!name.trim()) {
      setErr("请填写题目名（将作为 run 标识）");
      return;
    }
    if (!description.trim()) {
      setErr("请填写题面描述（求解器的解题提示）");
      return;
    }
    setBusy(true);
    setErr("");
    try {
      let id = runId;
      if (!id) {
        id = await createRun(name.trim());
        if (!id) throw new Error("创建 run 失败（后端不可达？）");
        setRunId(id);
      }
      const urlList = urls
        .split(/\n+/)
        .map((s) => s.trim())
        .filter((s) => s.startsWith("http://") || s.startsWith("https://"));
      await onStart(
        {
          mode: "manual",
          no_submit: true,
          prompt: description.trim(),
          challenge: {
            name: name.trim(),
            category: category.trim() || "manual",
            target: target.trim(),
            description: description.trim(),
            // 本地附件路径字符串 + 附件 URL 字符串混排；后端按 http(s) 前缀区分
            attachments: [...files.map((f) => f.path), ...urlList],
          },
        },
        id
      );
      onStarted(id);
    } catch (e) {
      setErr(`启动失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mlf">
      <div className="mlf-inner">
        <div>
          <div className="mlf-title">解题模式 · 新建题目</div>
          <div className="mlf-sub">
            人工填写题目信息与附件，后端在本地沙箱自行解题（不访问平台、不提交 flag）。
            完成后在对话区查看求解过程与 flag。
          </div>
        </div>

        <label className="mlf-field">
          <span className="mlf-label">题目名 *</span>
          <input
            className="mlf-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="如：web-unserialize-1-3"
            maxLength={80}
          />
        </label>

        <div className="mlf-grid">
          <label className="mlf-field">
            <span className="mlf-label">分类（可选）</span>
            <input
              className="mlf-input"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              placeholder="如：web / misc / crypto"
            />
          </label>
          <label className="mlf-field">
            <span className="mlf-label">目标地址（可选）</span>
            <input
              className="mlf-input"
              value={target}
              onChange={(e) => setTarget(e.target.value)}
              placeholder="如：http://10.0.0.5:8888"
            />
          </label>
        </div>

        <label className="mlf-field">
          <span className="mlf-label">题面描述 *（求解提示，可贴原题面）</span>
          <textarea
            className="mlf-textarea"
            value={description}
            onChange={(e) => setDescription(e.target.value)}
            placeholder="题面、考点提示、已知条件…"
          />
        </label>

        <div className="mlf-field">
          <span className="mlf-label">本地附件（先填题目名）</span>
          <label className="mlf-upload">
            <input ref={fileRef} type="file" multiple onChange={onPickFiles} disabled={!name.trim()} />
            {name.trim() ? "点击选择附件文件（可多选）" : "（请先在上方填写题目名）"}
          </label>
          {files.length > 0 && (
            <div className="mlf-chips">
              {files.map((f) => (
                <span className="mlf-chip" key={f.path} title={f.path}>
                  {f.name}
                  <button
                    className="mlf-chip-x"
                    onClick={() => removeFile(f.path)}
                    title="移除"
                    aria-label={`移除 ${f.name}`}
                  >
                    ✕
                  </button>
                </span>
              ))}
            </div>
          )}
        </div>

        <label className="mlf-field">
          <span className="mlf-label">附件下载 URL（每行一个）</span>
          <textarea
            className="mlf-textarea mlf-urls"
            value={urls}
            onChange={(e) => setUrls(e.target.value)}
            placeholder={"https://pro-resource.dasctf.com/xxx/file.zip\nhttps://example.com/challenge.tar.gz"}
          />
          <span className="mlf-hint">平台资源会自动带 X-Agent-AccessKey 重试；下载到本地 distfiles 供沙箱使用。</span>
        </label>

        {err && <div className="mlf-err">{err}</div>}

        <div className="mlf-actions">
          <button className="mlf-btn" onClick={onSubmit} disabled={busy}>
            {busy ? "启动中…" : "开始解题"}
          </button>
          <button className="mlf-btn ghost" onClick={onCancel} disabled={busy}>
            取消
          </button>
        </div>
      </div>
    </div>
  );
}
