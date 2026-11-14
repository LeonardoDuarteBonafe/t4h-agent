from dataclasses import dataclass
from pathlib import Path
import os
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')


@dataclass
class Settings:
    api_key: str = os.getenv('LLM_API_KEY', '')
    base_url: str = os.getenv('LLM_BASE_URL', 'https://litellm.tech4humans.com.br/v1').rstrip('/')
    model: str = 'gpt-4o-mini'
    mcp_url: str = os.getenv('MCP_URL', 'http://localhost:8765/mcp')
    run_id: str = os.getenv('RUN_ID', '').strip() or 'desenvolvimento-2026-11-14'
    host: str = os.getenv('APP_HOST', '127.0.0.1')
    port: int = int(os.getenv('APP_PORT', '8000'))
    auto_start: bool = os.getenv('AUTO_START', 'true').lower() == 'true'
    headless: bool = os.getenv('BROWSER_HEADLESS', 'true').lower() == 'true'
    data_dir: Path = ROOT / 'data'

    def __post_init__(self):
        if self.model != 'gpt-4o-mini' or os.getenv('LLM_MODEL', self.model) != 'gpt-4o-mini':
            raise ValueError('Este projeto utiliza exclusivamente gpt-4o-mini.')
        if self.base_url.endswith('/chat/completions'):
            self.base_url = self.base_url[:-len('/chat/completions')]
        self.data_dir.mkdir(exist_ok=True, parents=True)
