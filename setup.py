# Скрипт первичной установки. Ставит зависимости и браузеры Playwright,
# которые нужны actions/browser_control.py.

import subprocess
import sys

print("Installing requirements...")
subprocess.run(
    [sys.executable, "-m", "pip", "install", "-r", "requirements.txt"],
    check=True,
)

print("Installing Playwright browsers...")
subprocess.run([sys.executable, "-m", "playwright", "install"], check=True)

print("\nSetup complete. Run 'python main.py' to start Akli 2.0.")
