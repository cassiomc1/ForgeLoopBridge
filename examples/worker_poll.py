#!/usr/bin/env python3
"""
Exemplo mínimo de como o Operário pode monitorar o ForgeBridge.
Substitua a lógica de "executar tarefa" pelo seu agent (OpenCode, etc.).
"""

import time
import requests

BASE_URL = "http://localhost:8000"          # altere para a URL do seu ForgeBridge
WORKER_TOKEN = "operario_secret"            # mesmo token configurado no servidor
POLL_INTERVAL = 10                          # segundos


def post_status(content: str):
    r = requests.post(
        f"{BASE_URL}/api/messages",
        json={"token": WORKER_TOKEN, "content": content},
        timeout=15,
    )
    r.raise_for_status()
    print("Status postado com sucesso.")


def main():
    last_seen = 0.0
    print(f"Monitorando {BASE_URL} a cada {POLL_INTERVAL}s ...")

    while True:
        try:
            r = requests.get(
                f"{BASE_URL}/api/messages",
                params={"since": last_seen},
                timeout=15,
            )
            r.raise_for_status()
            messages = r.json()

            for msg in messages:
                last_seen = max(last_seen, msg["created_at"])

                if msg["role"] != "engineer":
                    continue

                print("\n" + "=" * 60)
                print("NOVA INSTRUÇÃO DO ENGENHEIRO")
                print("=" * 60)
                print(msg["content"])
                print("=" * 60 + "\n")

                # ────────────────────────────────────────────────
                # AQUI você chama o seu agent / OpenCode / script
                # Exemplo:
                #   result = run_opencode(msg["content"])
                #   pr_url = open_pull_request(...)
                # ────────────────────────────────────────────────

                # Depois de terminar, poste o status:
                post_status(
                    "### Status\n"
                    "Recebi a instrução e estou processando...\n\n"
                    "*(substitua esta mensagem pelo resultado real + link do PR)*"
                )

        except Exception as e:
            print(f"[erro] {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
