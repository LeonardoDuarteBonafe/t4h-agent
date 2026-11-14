import argparse
import atexit
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

from .config import Settings, ROOT


def main():
    parser = argparse.ArgumentParser(description='Time de triagem N1 com gpt-4o-mini')
    parser.add_argument('--with-world', action='store_true', help='Inicia o mundo se não houver serviço na porta configurada')
    args = parser.parse_args()
    settings = Settings()
    if args.with_world:
        from urllib.parse import urlsplit
        url = urlsplit(settings.mcp_url)
        port = url.port or 8765
        try:
            with socket.create_connection((url.hostname or 'localhost', port), timeout=1):
                active = True
        except OSError:
            active = False
        if not active:
            world_dir = ROOT.parent / 'Servidor MCP (Compartilhado)'
            command = [sys.executable, str(world_dir / 'mundo.py'), '--tema', 'triagem',
                       '--porta', str(port), '--erros', os.getenv('WORLD_ERRORS', '0'),
                       '--segundos-por-dia', os.getenv('WORLD_DAY_SECONDS', '1800'), '--sem-navegador']
            if os.getenv('WORLD_MANUAL', 'true').lower() == 'true':
                command.append('--sem-cenario')
            stream = open(settings.data_dir / 'mundo-processo.log', 'a', encoding='utf-8')
            world = subprocess.Popen(command, cwd=world_dir, stdout=stream, stderr=stream,
                                     creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0)
            def cleanup():
                if world.poll() is None:
                    world.terminate()
                    try:
                        world.wait(10)
                    except subprocess.TimeoutExpired:
                        world.kill()
                stream.close()
            atexit.register(cleanup)
            for _ in range(60):
                if world.poll() is not None:
                    raise SystemExit('O mundo não iniciou. Consulte data/mundo-processo.log.')
                try:
                    with socket.create_connection((url.hostname or 'localhost', port), timeout=.5):
                        break
                except OSError:
                    time.sleep(.25)
            else:
                raise SystemExit('O mundo não abriu a porta dentro do prazo.')
    import uvicorn
    print(f'Painel da solução: http://localhost:{settings.port}')
    print(f'Modelo dos três agentes: {settings.model}')
    uvicorn.run('triagem_n1.app:app', host=settings.host, port=settings.port, workers=1, log_level='warning')


if __name__ == '__main__':
    main()
