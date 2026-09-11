import os
import subprocess
from contracts import GreenLightConversionResult
from . import BaseTool


class GreenLightConverter(BaseTool):
    """GreenLightConverter 工具，將 HTML/CSS/JS 轉換為 Greenshift WordPress 區塊。"""

    def __init__(self, config):
        super().__init__(config)
        self.description = "將 HTML/CSS/JS 轉換為 Greenshift WordPress 區塊的轉換工具。"

    def _resolve_script(self, script_name: str) -> str:
        """解析腳本絕對路徑。"""
        project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        return os.path.join(project_root, "skills", "greenlight-vibe", "scripts", script_name)

    def _run(self, input_text: str, script: str) -> GreenLightConversionResult:
        """執行腳本：透過 stdin/stdout 傳遞資料。"""
        node_executable = "node"

        try:
            result = subprocess.run(
                [node_executable, script],
                input=input_text,
                capture_output=True,
                text=True,
                timeout=120,
            )

            stderr = result.stderr.strip()
            if result.returncode != 0:
                return GreenLightConversionResult(
                    success=False,
                    errors=[stderr or f"{os.path.basename(script)} exited with code {result.returncode}"],
                )

            output = result.stdout
            warnings = [stderr] if stderr else []
            return GreenLightConversionResult(success=True, blocks=output, warnings=warnings)

        except FileNotFoundError:
            return GreenLightConversionResult(
                success=False,
                errors=["Node.js executable not found. Please install Node.js and ensure it is on PATH."],
            )
        except subprocess.TimeoutExpired:
            return GreenLightConversionResult(
                success=False,
                errors=[f"{os.path.basename(script)} timed out after 120 seconds."],
            )
        except Exception as e:
            return GreenLightConversionResult(success=False, errors=[str(e)])

    def convert(self, html: str) -> GreenLightConversionResult:
        """將 HTML/CSS/JS 轉換為 Greenshift WordPress 區塊。"""
        script = self._resolve_script("convert.js")
        if not os.path.exists(script):
            return GreenLightConversionResult(
                success=False,
                errors=[f"convert.js not found at {script}"],
            )
        return self._run(html, script)

    def deconvert(self, blocks: str) -> GreenLightConversionResult:
        """將 Greenshift WordPress 區塊反向轉換為 HTML/CSS/JS。"""
        script = self._resolve_script("deconvert.js")
        if not os.path.exists(script):
            return GreenLightConversionResult(
                success=False,
                errors=[f"deconvert.js not found at {script}"],
            )
        return self._run(blocks, script)
