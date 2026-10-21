"""
Minerador estático de suítes de teste em repositórios Python do GitHub.

Script de apoio à Iniciação Científica: para cada URL de repositório listada
em `candidates.txt`, clona o projeto temporariamente, separa código de
produção de código de teste, mede LOC via `cloc`, e conta funções de teste
e fixtures customizadas via AST.
"""

import ast
import csv
import json
import logging
import os
import re
import shutil
import subprocess
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import requests

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DIRETORIO_REPOS = Path("./repositorios_temp")
ARQUIVO_URLS = Path("candidates.txt")
ARQUIVO_CSV = Path("mine_projects.csv")
CABECALHO_CSV = [
    "project_name", "descricao", "url", "num_tests", "num_fixtures",
    "loc_tests", "arquivos_tests", "loc_producao", "arquivos_producao",
    "loc_projeto_total", "motivo_exclusao",
]


@dataclass
class MetricasRepo:
    """Consolida as métricas estáticas extraídas de um repositório."""
    qtd_arquivos_teste: int
    loc_testes: int
    qtd_arquivos_producao: int
    loc_producao: int
    total_testes: int
    total_fixtures: int
    motivo_exclusao: str

    @property
    def loc_projeto_total(self) -> int:
        return self.loc_testes + self.loc_producao

    @property
    def elegivel_amostra(self) -> bool:
        return not self.motivo_exclusao

    def as_row(self, nome_repo: str, descricao: str, url_repo: str) -> list:
        """Monta a linha do CSV na mesma ordem de CABECALHO_CSV."""
        return [
            nome_repo, descricao, url_repo,
            self.total_testes, self.total_fixtures,
            self.loc_testes, self.qtd_arquivos_teste,
            self.loc_producao, self.qtd_arquivos_producao,
            self.loc_projeto_total, self.motivo_exclusao,
        ]


class ClonagemFalhouError(Exception):
    """Levantada quando o `git clone` falha, para a linha não entrar no CSV como zero."""


def coletar_metadados_github(dono_repo: str, nome_repo: str) -> str:
    """Busca a descrição do projeto na API do GitHub usando token de variável de ambiente."""
    url_api = f"https://api.github.com/repos/{dono_repo}/{nome_repo}"
    github_token = os.environ.get("GITHUB_TOKEN")

    headers = {"User-Agent": "Script-Analise-IC-Python"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"

    try:
        resposta = requests.get(url_api, headers=headers, timeout=10)
        if resposta.status_code == 200:
            return resposta.json().get("description") or "Sem descrição"
        elif resposta.status_code == 403:
            logger.warning(f"Limite da API (403) para {nome_repo}. Configure o GITHUB_TOKEN.")
        else:
            logger.warning(f"GitHub API retornou {resposta.status_code} para {nome_repo}.")
    except requests.RequestException as e:
        logger.warning(f"Falha ao buscar metadados de {nome_repo}: {e}")

    return "Sem descrição"


def rodar_cloc(lista_arquivos: list[Path]) -> tuple[int, int]:
    """Chama o cloc no terminal passando uma lista de arquivos e retorna (num_arquivos, loc)."""
    if not lista_arquivos:
        return 0, 0

    caminhos = [str(arq) for arq in lista_arquivos]
    comando = ["cloc", "--json"] + caminhos

    resultado = subprocess.run(comando, capture_output=True, text=True)

    try:
        dados = json.loads(resultado.stdout)
        if "Python" in dados:
            return dados["Python"]["nFiles"], dados["Python"]["code"]
    except json.JSONDecodeError:
        logger.warning("Não foi possível interpretar a saída do cloc.")

    return 0, 0


def parse_arquivo_python(caminho: Path) -> ast.Module | None:
    """Lê e parseia um arquivo Python via AST, suprimindo SyntaxWarning de código de terceiros.

    Código de repositórios minerados frequentemente contém sequências de escape
    inválidas (`"\\d"` em vez de `r"\\d"`), que o CPython tolera mas sinaliza com
    SyntaxWarning — isso não impede o parsing nem afeta as métricas, só polui o
    log em massa. Passa `filename` real para que qualquer aviso que sobreviva
    aponte para o arquivo de origem em vez de `<unknown>`. Retorna None em caso
    de encoding inválido ou erro de sintaxe real (SyntaxError).
    """
    try:
        codigo_fonte = caminho.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return None

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(codigo_fonte, filename=str(caminho))
    except SyntaxError:
        return None


def classificar_arquivos(caminho_repo: Path) -> tuple[list[Path], list[Path]]:
    """Lista os .py do repositório e separa arquivos de teste dos de produção."""
    todos_py = [
        f for f in caminho_repo.rglob("*.py")
        if not any(parte.startswith(".") for parte in f.parts)
    ]

    arquivos_teste = [
        f for f in todos_py
        if f.name.startswith("test_") or f.name.endswith("_test.py") or f.name == "conftest.py"
    ]
    arquivos_producao = [f for f in todos_py if f not in arquivos_teste]

    return arquivos_teste, arquivos_producao


def contar_fixtures(arquivos: list[Path]) -> int:
    """Conta funções decoradas com fixture do pytest via AST (ignora comentários e strings).

    Cobre tanto `@pytest.fixture` / `@pytest.fixture(...)` (ast.Attribute) quanto
    `@fixture` / `@fixture(...)` (ast.Name), caso do `from pytest import fixture`.
    Nota: a checagem do `ast.Name` é uma heurística por nome (não resolve a origem
    do import), então um decorator local chamado `fixture` sem relação com o pytest
    geraria um falso positivo raro.
    """
    total = 0
    for arquivo in arquivos:
        arvore = parse_arquivo_python(arquivo)
        if arvore is None:
            continue

        for no in ast.walk(arvore):
            if not isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for decorator in no.decorator_list:
                alvo = decorator.func if isinstance(decorator, ast.Call) else decorator
                eh_atributo = isinstance(alvo, ast.Attribute) and alvo.attr == "fixture"
                eh_nome = isinstance(alvo, ast.Name) and alvo.id == "fixture"
                if eh_atributo or eh_nome:
                    total += 1
                    break
    return total


def contar_funcoes_teste(arquivos_teste: list[Path]) -> int:
    """Conta funções de teste (test_*) via AST, incluindo `async def` (pytest-asyncio)."""
    total = 0
    for arquivo in arquivos_teste:
        arvore = parse_arquivo_python(arquivo)
        if arvore is None:
            continue
        for no in ast.walk(arvore):
            if isinstance(no, (ast.FunctionDef, ast.AsyncFunctionDef)) and no.name.startswith("test_"):
                total += 1
    return total


PADRAO_DEPENDENCIA_PYTEST = re.compile(r"^pytest\s*([\[=<>~!@].*)?$")

ARQUIVOS_DEPENDENCIA_RAIZ = [
    "requirements.txt", "requirements-dev.txt", "requirements-test.txt",
    "pyproject.toml", "setup.py", "setup.cfg", "tox.ini", "Pipfile",
]


def _arquivos_dependencia(caminho_repo: Path) -> list[Path]:
    """Lista candidatos a arquivo de dependência: raiz + subpasta requirements/ (um nível).

    Cobre o padrão comum de projetos maiores (ex.: ecossistema NetBox) que dividem
    requirements em requirements/base.txt, requirements/test.txt etc., em vez de
    manter tudo solto na raiz.
    """
    candidatos = [caminho_repo / nome for nome in ARQUIVOS_DEPENDENCIA_RAIZ]
    pasta_requirements = caminho_repo / "requirements"
    if pasta_requirements.is_dir():
        candidatos.extend(pasta_requirements.glob("*.txt"))
    return candidatos


def _declara_pytest_em_arquivo_dependencia(caminho_repo: Path) -> bool:
    """Verifica se algum arquivo de dependência declara pytest como pacote, ignorando comentários."""
    for caminho in _arquivos_dependencia(caminho_repo):
        if not caminho.exists():
            continue
        try:
            linhas = caminho.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for linha in linhas:
            linha = linha.strip()
            if not linha or linha.startswith("#") or linha.startswith(";"):
                continue
            linha = linha.rstrip(",").strip().strip('"').strip("'").strip().lower()
            if PADRAO_DEPENDENCIA_PYTEST.match(linha):
                return True
    return False


def _importa_pytest_no_codigo(caminho_repo: Path) -> bool:
    """Verifica se algum arquivo .py do repositório importa pytest de fato (via AST)."""
    for arquivo in caminho_repo.rglob("*.py"):
        if any(parte.startswith(".") for parte in arquivo.parts):
            continue
        arvore = parse_arquivo_python(arquivo)
        if arvore is None:
            continue
        for no in ast.walk(arvore):
            if isinstance(no, ast.Import) and any(
                alias.name.split(".")[0] == "pytest" for alias in no.names
            ):
                return True
            if isinstance(no, ast.ImportFrom) and no.module and no.module.split(".")[0] == "pytest":
                return True
    return False


def confirma_uso_pytest(caminho_repo: Path) -> bool:
    """Revalida localmente se o repositório realmente usa pytest.

    Mais confiável que o substring solto usado pelo script de busca de candidatos:
    respeita limite de palavra e ignora comentários na checagem de dependência, e
    ainda confirma via import real no código como segunda fonte de evidência.
    """
    return (
        _declara_pytest_em_arquivo_dependencia(caminho_repo)
        or _importa_pytest_no_codigo(caminho_repo)
    )


def gitignore_pode_ocultar_testes(caminho_repo: Path) -> bool:
    """Heurística: verifica se o .gitignore do repositório exclui algo com 'test' no nome.

    Não prova que testes foram omitidos do controle de versão — apenas sinaliza
    repositórios que merecem checagem manual antes de interpretar `arquivos_tests`
    baixo (ou zero) como "projeto sem testes".
    """
    caminho_gitignore = caminho_repo / ".gitignore"
    if not caminho_gitignore.exists():
        return False
    try:
        conteudo = caminho_gitignore.read_text(encoding="utf-8").lower()
    except UnicodeDecodeError:
        return False
    return "test" in conteudo


def avaliar_motivo_exclusao(
    caminho_repo: Path, qtd_arquivos_teste: int, total_testes: int, total_fixtures: int
) -> str:
    """Decide, de forma consolidada, se o candidato fica de fora da amostra e por quê.

    Retorna string vazia quando o candidato é elegível. Checa, nessa ordem:
    (1) se o uso de pytest se confirma localmente (dependência declarada ou import real);
    (2) se nenhum arquivo de teste foi encontrado — e, nesse caso, se o .gitignore
    sugere que testes podem ter sido deliberadamente excluídos do controle de versão;
    (3) se arquivos de teste foram encontrados, mas nenhuma função test_* foi detectada
    dentro deles — sinal de convenção de descoberta não padrão (ex.: plugins como
    pytest-relaxed, usados no ecossistema Fabric/Invoke) OU de uma limitação ainda não
    identificada do próprio script; requer checagem manual antes de assumir a causa;
    (4) se nenhuma fixture (@pytest.fixture) foi detectada — exigência explícita da
    proposta do projeto: candidatos sem fixture não interessam à amostra.
    """
    if not confirma_uso_pytest(caminho_repo):
        return "pytest não confirmado"

    if qtd_arquivos_teste == 0:
        if gitignore_pode_ocultar_testes(caminho_repo):
            return "sem arquivos de teste (possível exclusão via .gitignore)"
        return "sem arquivos de teste"

    if total_testes == 0:
        return "sem funções test_* (causa não confirmada — revisar manualmente)"

    if total_fixtures == 0:
        return "sem fixtures declaradas"

    return ""


def contar_metricas_estaticas_testes(caminho_repo: Path) -> MetricasRepo:
    """Orquestra a extração de todas as métricas estáticas de um repositório clonado."""
    arquivos_teste, arquivos_producao = classificar_arquivos(caminho_repo)
    todos_py = arquivos_teste + arquivos_producao

    qtd_arquivos_teste, loc_testes = rodar_cloc(arquivos_teste)
    qtd_arquivos_producao, loc_producao = rodar_cloc(arquivos_producao)

    total_fixtures = contar_fixtures(todos_py)
    total_testes = contar_funcoes_teste(arquivos_teste)
    motivo_exclusao = avaliar_motivo_exclusao(
        caminho_repo, qtd_arquivos_teste, total_testes, total_fixtures
    )

    return MetricasRepo(
        qtd_arquivos_teste=qtd_arquivos_teste,
        loc_testes=loc_testes,
        qtd_arquivos_producao=qtd_arquivos_producao,
        loc_producao=loc_producao,
        total_testes=total_testes,
        total_fixtures=total_fixtures,
        motivo_exclusao=motivo_exclusao,
    )


def analisar_candidato(url_repo: str, diretorio_base: Path) -> list:
    """Clona, analisa e limpa um único repositório candidato, retornando a linha do CSV."""
    partes = url_repo.rstrip("/").split("/")
    dono_repo, nome_repo = partes[-2], partes[-1]

    descricao = coletar_metadados_github(dono_repo, nome_repo)
    caminho_repo = diretorio_base / nome_repo

    if not caminho_repo.exists():
        resultado_clone = subprocess.run(
            ["git", "clone", "--depth", "1", url_repo, str(caminho_repo)],
            capture_output=True, text=True,
        )
        if resultado_clone.returncode != 0:
            raise ClonagemFalhouError(
                resultado_clone.stderr.strip() or "git clone falhou sem mensagem de erro"
            )

    metricas = contar_metricas_estaticas_testes(caminho_repo)
    return metricas.as_row(nome_repo, descricao, url_repo)


def carregar_urls_processadas(arquivo_csv: Path) -> set[str]:
    """Lê o CSV existente (se houver) e retorna o conjunto de URLs já processadas."""
    urls_processadas = set()
    if not arquivo_csv.exists():
        return urls_processadas

    with open(arquivo_csv, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader, None)  # pula cabeçalho
        for linha in reader:
            if len(linha) >= 3:
                urls_processadas.add(linha[2])

    return urls_processadas


def resumir_amostra(arquivo_csv: Path) -> None:
    """Lê o CSV final e loga quantos candidatos são elegíveis e a distribuição de exclusões.

    Lê o arquivo inteiro (não só o que foi processado nesta execução), então reflete
    o estado acumulado da amostra mesmo em execuções retomadas.
    """
    if not arquivo_csv.exists():
        return

    with open(arquivo_csv, encoding="utf-8") as f:
        linhas = list(csv.DictReader(f))

    total = len(linhas)
    elegiveis = sum(1 for l in linhas if not l["motivo_exclusao"].strip())
    motivos = Counter(l["motivo_exclusao"] for l in linhas if l["motivo_exclusao"].strip())

    logger.info(f"Amostra: {elegiveis} elegíveis de {total} candidatos processados.")
    for motivo, qtd in motivos.most_common():
        logger.info(f"  excluídos ({qtd}): {motivo}")


def limpar_clone(diretorio_base: Path, url_repo: str) -> None:
    """Remove o diretório clonado de um repositório, ignorando erros."""
    nome_repo = url_repo.rstrip("/").split("/")[-1]
    caminho_repo = diretorio_base / nome_repo
    if caminho_repo.exists():
        shutil.rmtree(caminho_repo, ignore_errors=True)


def main() -> None:
    DIRETORIO_REPOS.mkdir(exist_ok=True)

    if not ARQUIVO_URLS.exists():
        logger.error("Crie um arquivo 'candidates.txt' com as URLs dos repositórios.")
        return

    urls_candidatos = [
        linha.strip() for linha in ARQUIVO_URLS.read_text().splitlines() if linha.strip()
    ]

    urls_ja_processadas = carregar_urls_processadas(ARQUIVO_CSV)
    # dict.fromkeys preserva a ordem original e remove duplicatas dentro do
    # próprio candidates.txt, que senão seriam processadas (e escritas) mais de uma vez.
    novos_para_processar = list(dict.fromkeys(
        url for url in urls_candidatos if url not in urls_ja_processadas
    ))

    if not novos_para_processar:
        logger.info("Nenhum repositório novo para analisar. Saindo...")
        resumir_amostra(ARQUIVO_CSV)
        return

    modo_abertura = "a" if ARQUIVO_CSV.exists() else "w"

    with open(ARQUIVO_CSV, modo_abertura, newline="", encoding="utf-8") as f:
        writer = csv.writer(f)

        if modo_abertura == "w":
            writer.writerow(CABECALHO_CSV)

        for url in novos_para_processar:
            # Segunda camada de proteção contra duplicata: cobre o caso de
            # uma URL reaparecer em candidates.txt de um jeito que o
            # dict.fromkeys acima não capturaria (ex.: normalização futura).
            if url in urls_ja_processadas:
                logger.info(f"Já processado nesta execução, pulando: {url}")
                continue

            logger.info(f"Analisando: {url}...")
            try:
                dados = analisar_candidato(url, DIRETORIO_REPOS)
                writer.writerow(dados)
                f.flush()
                urls_ja_processadas.add(url)
            except ClonagemFalhouError as e:
                logger.warning(f"Pulando {url} — falha ao clonar: {e}")
            except Exception as e:
                logger.error(f"Erro ao analisar {url}: {e}")
            finally:
                limpar_clone(DIRETORIO_REPOS, url)

    logger.info("Análise concluída!")
    resumir_amostra(ARQUIVO_CSV)


if __name__ == "__main__":
    main()