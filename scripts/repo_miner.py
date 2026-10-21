#!/usr/bin/env python3
"""
Minerador de Repositórios GitHub - Busca de Evidências do Pytest.

Funcionalidades de Engenharia de Software Avançadas:
- Tolerância a falhas (Rate Limit handling) dinâmica.
- Curto-circuito na detecção (retorna na primeira evidência).
- Suporte a paginação customizada (--start-page) para retomada de buscas.
- Suporte a faixa de estrelas (--max-stars), para contornar o teto de 1000
  resultados por query da API de busca do GitHub rodando uma query por faixa.
- Escrita de arquivos segura em modo Append, com deduplicação por URL.
- Tratamento explícito do 422 (Validation Failed) que a API retorna ao
  tentar paginar além do teto de 1000 resultados, em vez de deixar propagar.
- Logging estruturado.
"""

import argparse
import base64
import csv
import datetime
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Optional

import requests

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None  # Fallback gracefully se for Python < 3.11


# Configuração de Logging Profissional
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

# Constantes da API e Regras de Negócio
GITHUB_API = "https://api.github.com"
API_VERSION = "2026-03-10"

PADRAO_DEPENDENCIA_LINHA = re.compile(r"^pytest\s*([\[=<>~!@].*)?$")
PADRAO_PYTEST_PALAVRA = re.compile(r"\bpytest\b")

ARQUIVOS_DEPENDENCIA_RAIZ = [
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
    "setup.py", "setup.cfg", "tox.ini", "Pipfile",
]

CAMPOS_CSV = [
    "name", "full_name", "url", "description", "stars", "forks",
    "language", "open_issues", "last_update", "created_at",
    "license", "pytest_evidence",
]


class LimitePaginaBuscaError(Exception):
    """A API de busca do GitHub nunca retorna além de 1000 resultados por
    query (10 páginas de 100). Pedir além disso não dá 403, dá 422."""


class GitHubClient:
    """Cliente HTTP com gestão autônoma de Rate Limits da API do GitHub."""

    def __init__(self, token: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
        })
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def get(self, endpoint: str, params: Optional[dict[str, Any]] = None) -> dict:
        url = f"{GITHUB_API}{endpoint}"

        while True:
            response = self.session.get(url, params=params, timeout=30)

            if response.status_code == 422:
                raise LimitePaginaBuscaError(
                    f"GitHub recusou a página (422) — provável teto de 1000 "
                    f"resultados por query. Detalhe: {response.text[:200]}"
                )

            if response.status_code == 403:
                retry_after = response.headers.get("Retry-After")
                remaining = response.headers.get("X-RateLimit-Remaining")
                reset = response.headers.get("X-RateLimit-Reset")

                # Limite secundário (Abuse Rate Limit - rajadas)
                if retry_after:
                    espera = int(retry_after) + 2
                    logger.warning(f"Limite secundário atingido. Pausando por {espera}s...")
                    time.sleep(espera)
                    continue

                # Limite primário (Cota do Token esgotada)
                if remaining == "0" and reset:
                    reset_time = int(reset)
                    agora = int(time.time())
                    espera = max(reset_time - agora, 0) + 5

                    hora_retorno = datetime.datetime.fromtimestamp(reset_time).strftime('%H:%M:%S')
                    logger.error(f"Cota da API esgotada. Pausando por {espera}s até o reset às {hora_retorno}...")
                    time.sleep(espera)
                    continue

            response.raise_for_status()
            return response.json()

    def search_repositories(self, query: str, start_page: int = 1, pages: int = 5, per_page: int = 100) -> list[dict]:
        repositories = []
        end_page = start_page + pages

        for page in range(start_page, end_page):
            logger.info(f"Buscando repositórios: página {page} (até {end_page - 1})...")

            try:
                data = self.get(
                    "/search/repositories",
                    params={
                        "q": query, "sort": "stars", "order": "desc",
                        "page": page, "per_page": per_page,
                    },
                )
            except LimitePaginaBuscaError as exc:
                logger.warning(
                    f"{exc} Pare de pedir mais páginas para esta query — use "
                    f"--max-stars para dividir em faixas menores e continuar coletando."
                )
                break

            items = data.get("items", [])
            repositories.extend(items)

            if len(items) < per_page:
                break

            time.sleep(2)  # Previne rajadas entre páginas pesadas de busca

        return repositories

    def get_contents(self, owner: str, repo: str, path: str) -> Optional[dict | list]:
        try:
            return self.get(f"/repos/{owner}/{repo}/contents/{path}")
        except requests.HTTPError as exc:
            if exc.response.status_code == 404:
                return None
            raise

    def get_file_text(self, owner: str, repo: str, path: str) -> Optional[str]:
        conteudo = self.get_contents(owner, repo, path)
        if not isinstance(conteudo, dict) or conteudo.get("type") != "file":
            return None
        try:
            return base64.b64decode(conteudo["content"]).decode("utf-8", errors="ignore")
        except Exception:
            return None


def linha_declara_pytest(linha: str) -> bool:
    linha = linha.strip()
    if not linha or linha.startswith("#") or linha.startswith(";"):
        return False
    linha = linha.rstrip(",").strip().strip('"').strip("'").strip().lower()
    return bool(PADRAO_DEPENDENCIA_LINHA.match(linha))


def texto_menciona_pytest(conteudo: str) -> bool:
    for linha in conteudo.splitlines():
        bruta = linha.strip()
        if bruta.startswith("#"):
            continue
        if PADRAO_PYTEST_PALAVRA.search(linha):
            return True
    return False


def _normaliza_nome_pacote(especificador: str) -> str:
    return re.split(r"[\[<>=~!@\s;]", especificador.strip(), maxsplit=1)[0].strip().lower()


def extrair_pacotes_pyproject(dados: dict) -> set[str]:
    nomes: set[str] = set()

    projeto = dados.get("project", {}) or {}
    for item in projeto.get("dependencies", []) or []:
        nomes.add(_normaliza_nome_pacote(item))
    for grupo in (projeto.get("optional-dependencies", {}) or {}).values():
        for item in grupo or []:
            nomes.add(_normaliza_nome_pacote(item))

    tool = dados.get("tool", {}) or {}
    poetry = tool.get("poetry", {}) or {}
    for chave in ("dependencies", "dev-dependencies"):
        for nome in (poetry.get(chave, {}) or {}).keys():
            nomes.add(_normaliza_nome_pacote(nome))
    for grupo in (poetry.get("group", {}) or {}).values():
        for nome in ((grupo or {}).get("dependencies", {}) or {}).keys():
            nomes.add(_normaliza_nome_pacote(nome))

    return nomes


# --- Cadeia de Verificação (Short-Circuit) ---

def checar_requirements_raiz(client: GitHubClient, owner: str, repo: str) -> list[str]:
    for nome in ARQUIVOS_DEPENDENCIA_RAIZ:
        conteudo = client.get_file_text(owner, repo, nome)
        if conteudo is None:
            continue
        encontrado = (
            texto_menciona_pytest(conteudo) if nome in ("setup.py", "setup.cfg", "tox.ini")
            else any(linha_declara_pytest(linha) for linha in conteudo.splitlines())
        )
        if encontrado:
            return [nome]
    return []


def checar_requirements_subpasta(client: GitHubClient, owner: str, repo: str) -> list[str]:
    listagem = client.get_contents(owner, repo, "requirements")
    if not isinstance(listagem, list):
        return []

    for item in listagem:
        if item.get("type") != "file" or not item.get("name", "").endswith(".txt"):
            continue
        caminho = f"requirements/{item['name']}"
        conteudo = client.get_file_text(owner, repo, caminho)
        if conteudo and any(linha_declara_pytest(linha) for linha in conteudo.splitlines()):
            return [caminho]
    return []


def checar_pyproject_toml(client: GitHubClient, owner: str, repo: str) -> list[str]:
    conteudo = client.get_file_text(owner, repo, "pyproject.toml")
    if conteudo is None:
        return []

    if tomllib is not None:
        try:
            dados = tomllib.loads(conteudo)
            if "pytest" in extrair_pacotes_pyproject(dados):
                return ["pyproject.toml"]
            return []
        except tomllib.TOMLDecodeError:
            pass

    return ["pyproject.toml"] if texto_menciona_pytest(conteudo) else []


def checar_workflows_ci(client: GitHubClient, owner: str, repo: str) -> list[str]:
    listagem = client.get_contents(owner, repo, ".github/workflows")
    if not isinstance(listagem, list):
        return []

    for item in listagem:
        nome = item.get("name", "")
        if item.get("type") != "file" or not (nome.endswith(".yml") or nome.endswith(".yaml")):
            continue
        conteudo = client.get_file_text(owner, repo, f".github/workflows/{nome}")
        if conteudo and texto_menciona_pytest(conteudo):
            return [f"workflow:{nome}"]
    return []


def detect_pytest_usage(client: GitHubClient, repository: dict[str, Any]) -> list[str]:
    owner = repository["owner"]["login"]
    repo = repository["name"]

    evidencia = checar_requirements_raiz(client, owner, repo)
    if evidencia: return evidencia

    evidencia = checar_requirements_subpasta(client, owner, repo)
    if evidencia: return evidencia

    evidencia = checar_pyproject_toml(client, owner, repo)
    if evidencia: return evidencia

    return checar_workflows_ci(client, owner, repo)


def carregar_urls_existentes(caminho_saida: Path) -> set[str]:
    """Lê o CSV de saída, se já existir, para não duplicar URL no modo append."""
    if not caminho_saida.is_file():
        return set()
    with caminho_saida.open(encoding="utf-8") as f:
        return {linha["url"] for linha in csv.DictReader(f) if linha.get("url")}


def import_token_from_env() -> str:
    return os.getenv("GITHUB_TOKEN", "")


def main():
    parser = argparse.ArgumentParser(description="Busca iterativa de projetos no GitHub que utilizam Pytest.")
    parser.add_argument("--token", default=import_token_from_env(), help="GitHub Personal Access Token.")
    parser.add_argument("--min-stars", type=int, default=50, help="Número mínimo de estrelas. Default: 50.")
    parser.add_argument(
        "--max-stars", type=int, default=None,
        help="Número máximo de estrelas (faixa fechada com --min-stars). Use para "
             "dividir a coleta em faixas e contornar o teto de 1000 resultados por "
             "query da API de busca do GitHub — ex.: rodar min=50/max=99, depois "
             "min=100/max=249, depois min=250 sem max, etc.",
    )
    parser.add_argument("--language", default="Python", help="Linguagem do projeto. Default: Python.")

    parser.add_argument("--start-page", type=int, default=1, help="Página inicial na API do GitHub. Default: 1.")
    parser.add_argument("--pages", type=int, default=5, help="Quantidade de páginas para buscar a partir do start-page. Default: 5.")

    parser.add_argument("--output", default="results/github_pytest_search.csv", help="Caminho do arquivo CSV de saída.")
    args = parser.parse_args()

    client = GitHubClient(args.token)

    faixa_estrelas = (
        f"stars:{args.min_stars}..{args.max_stars}" if args.max_stars is not None
        else f"stars:>={args.min_stars}"
    )
    query = f"language:{args.language} {faixa_estrelas} fork:false archived:false"
    logger.info(f"Iniciando query no GitHub: {query}")

    repositories = client.search_repositories(query=query, start_page=args.start_page, pages=args.pages)

    if not repositories:
        logger.warning("Nenhum repositório encontrado para esta janela de busca.")
        return

    logger.info(f"Candidatos brutos recuperados da busca: {len(repositories)}")

    caminho_saida = Path(args.output)
    urls_existentes = carregar_urls_existentes(caminho_saida)
    if urls_existentes:
        logger.info(f"{len(urls_existentes)} URLs já presentes em {caminho_saida} — serão puladas se reaparecerem.")

    results = []
    for index, repository in enumerate(repositories, start=1):
        full_name = repository["full_name"]
        url_repo = repository["html_url"]

        if url_repo in urls_existentes:
            logger.info(f"[{index}/{len(repositories)}] Já presente no CSV, pulando: {full_name}")
            continue

        logger.info(f"[{index}/{len(repositories)}] Inspecionando {full_name}...")

        try:
            evidence = detect_pytest_usage(client, repository)
        except requests.HTTPError as exc:
            logger.error(f"Falha na API para {full_name}: {exc}")
            evidence = None
        finally:
            time.sleep(1.0)  # Throttling preventivo — agora roda mesmo quando a análise falha

        if not evidence:
            continue

        urls_existentes.add(url_repo)
        results.append({
            "name": repository["name"],
            "full_name": repository["full_name"],
            "url": url_repo,
            "description": repository["description"] or "",
            "stars": repository["stargazers_count"],
            "forks": repository["forks_count"],
            "language": repository["language"],
            "open_issues": repository["open_issues_count"],
            "last_update": repository["updated_at"],
            "created_at": repository["created_at"],
            "license": repository["license"]["spdx_id"] if repository["license"] else "",
            "pytest_evidence": ", ".join(evidence),
        })

    if not results:
        logger.info("Nenhum projeto novo utilizava pytest nesta remessa.")
        return

    # Escrita inteligente (Append Mode + Pathlib + dedup por URL já aplicado acima)
    caminho_saida.parent.mkdir(parents=True, exist_ok=True)
    arquivo_ja_existia = caminho_saida.is_file()

    with caminho_saida.open(mode="a", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=CAMPOS_CSV)

        if not arquivo_ja_existia:
            writer.writeheader()

        writer.writerows(results)

    logger.info("Processamento finalizado com sucesso!")
    logger.info(f"Novos projetos validados: {len(results)}")
    logger.info(f"Dados anexados em: {caminho_saida.absolute()}")


if __name__ == "__main__":
    main()