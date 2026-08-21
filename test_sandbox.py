import asyncio
from backend.sandbox import configure_semaphore, DockerSandbox

async def main():
    # 初始化沙箱并发
    configure_semaphore(max_concurrent=5)

    # !!! 重点：镜像名称和你日志一致 ctf-sandbox
    sandbox = DockerSandbox(
        image="ctf-sandbox",
        # 这里指向存放题目distfiles、metadata.yml的目录
        challenge_dir="/tmp",
        memory_limit="4g"
    )

    try:
        await sandbox.start()
        print(f"✅ 沙箱启动成功 ID:{sandbox.container_id}")

        # 这里替换成你的exp/解题命令
        cmd = """
        ls -la /challenge
        # 在这里执行利用脚本，尝试读取flag
        cat /tmp/metadata.yml
        """
        res = await sandbox.exec(cmd, timeout_s=120)

        print("\n===== Command Result =====")
        print(f"ExitCode: {res.exit_code}")
        print(f"STDOUT:\n{res.stdout}")
        print(f"STDERR:\n{res.stderr}")

    finally:
        # 自动销毁容器
        await sandbox.stop()
        print("\n✅ 沙箱已销毁")

if __name__ == "__main__":
    asyncio.run(main())
