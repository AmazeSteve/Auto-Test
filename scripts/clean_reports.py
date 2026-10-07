from pathlib import Path

import shutil

ROOT_DIR = Path(__file__).resolve().parents[1]

REPORT_DIRS = [
    ROOT_DIR / "reports" / "allure-results",
    ROOT_DIR / "reports" / "allure-report",
]

def clean_reports():
    for path in REPORT_DIRS:
        if path.exists():
            shutil.rmtree(path)
        path.mkdir(parents=True,exist_ok=True)
        print(f"已清理并创建目录:{path}")

if __name__ == "__main__":
    clean_reports()