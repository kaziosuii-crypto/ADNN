"""启动 ADNN 控制台：Node.js 网页负责渲染，Python 子进程负责跑网络。"""

import os
import subprocess
import sys
import threading
import time
import webbrowser

ROOT = os.path.dirname(os.path.abspath(__file__))
PORT = os.environ.get("BTB_PORT", "8420")


def main():
    if "--open" in sys.argv[1:]:
        def open_browser():
            time.sleep(1.5)
            webbrowser.open("http://localhost:%s/" % PORT)

        threading.Thread(target=open_browser, daemon=True).start()

    subprocess.run(["node", os.path.join(ROOT, "web", "server.js")], cwd=ROOT, check=False)


if __name__ == "__main__":
    main()
