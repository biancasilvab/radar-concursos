"""Radar de concursos: PCI MCP -> filtros -> SQLite -> Telegram.

Importante: o PCI MCP documenta buscas de concursos com inscrições abertas.
Concursos apenas autorizados/previstos não são fornecidos por essas ferramentas;
eles ficam configurados como objetivo futuro e não são anunciados como monitorados.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
DB_PATH = os.path.join(BASE_DIR, "concursos.db")
PCI_MCP_URL = "https://mcp.pciconcursos.com.br/mcp"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("radar-concursos")
load_dotenv(os.path.join(BASE_DIR, ".env"))


def load_config() -> dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", value.casefold()).strip()


def init_db() -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("""CREATE TABLE IF NOT EXISTS concursos (
            fingerprint TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            url TEXT,
            summary TEXT,
            location TEXT,
            source TEXT NOT NULL,
            first_seen_utc TEXT NOT NULL,
            notified INTEGER NOT NULL DEFAULT 0,
            keyword_matches TEXT NOT NULL,
            eligibility TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'inscricoes_abertas'
        )""")
        con.commit()


def telegram_send(text: str) -> bool:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.error("Configure TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID no .env")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id.strip(), "text": text, "disable_web_page_preview": True},
            timeout=20,
        )
        data = r.json()
        if not data.get("ok"):
            log.error("Telegram recusou a mensagem: %s", data.get("description", "erro sem descrição"))
            return False
        return True
    except (requests.RequestException, ValueError) as exc:
        log.error("Falha ao enviar mensagem ao Telegram: %s", exc)
        return False


def tool_search_argument(schema: dict[str, Any], term: str, config: dict[str, Any]) -> dict[str, Any]:
    """Adapta os argumentos às propriedades reais expostas pelo servidor MCP."""
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    args: dict[str, Any] = {}
    search_names = ("cargo", "termo", "query", "pesquisa", "texto", "busca", "nome", "q")
    for name in search_names:
        if name in props:
            args[name] = term
            break
    # Busca por cidade/estado só é aplicada quando o servidor aceita esses campos.
    for name in props:
        low = name.casefold()
        if low in ("uf", "estado", "state") and config["localizacao"].get("estados"):
            args[name] = config["localizacao"]["estados"][0].lower()
        elif low in ("cidade", "city") and config["localizacao"].get("cidades"):
            args[name] = config["localizacao"]["cidades"][0]
        elif low in ("regiao", "região", "region") and config["localizacao"].get("abrangencia") == "Brasil":
            # Brasil inteiro: não restringir a uma região.
            pass
        elif low in ("apenas_professores", "somente_professores", "teachers_only"):
            args[name] = False
    # Preenche somente parâmetros obrigatórios simples que não sejam o termo de busca.
    for name in required:
        if name in args:
            continue
        spec = props.get(name, {})
        typ = spec.get("type")
        if typ == "boolean":
            args[name] = False
        elif typ == "integer":
            args[name] = min(int(config.get("limite_resultados_por_busca", 50)), int(spec.get("maximum", 100)))
        elif typ == "string":
            # Evita inventar filtros obrigatórios: falha explícita é melhor que uma consulta enganosa.
            if name in ("cargo", "termo", "query", "pesquisa", "texto", "busca", "nome", "q"):
                args[name] = term
    return args


def result_to_text(result: Any) -> str:
    pieces: list[str] = []
    for item in getattr(result, "content", []) or []:
        text_value = getattr(item, "text", None)
        if text_value:
            pieces.append(text_value)
        elif hasattr(item, "model_dump"):
            pieces.append(json.dumps(item.model_dump(), ensure_ascii=False))
    structured = getattr(result, "structuredContent", None)
    if structured:
        pieces.append(json.dumps(structured, ensure_ascii=False))
    if not pieces:
        try:
            return json.dumps(result.model_dump(), ensure_ascii=False, default=str)
        except Exception:
            return str(result)
    return "\n".join(pieces)


def extract_candidates(raw: str, source: str, search_term: str) -> list[dict[str, str]]:
    """Converte respostas MCP JSON/texto em candidatos, sem presumir elegibilidade."""
    candidates: list[dict[str, str]] = []
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        parsed = None

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            lower = {str(k).casefold(): v for k, v in obj.items()}
            title = next((str(lower[k]) for k in ("title", "titulo", "título", "name", "nome", "cargo") if lower.get(k)), "")
            url = next((str(lower[k]) for k in ("url", "link", "edital_url", "href") if lower.get(k)), "")
            summary = next((str(lower[k]) for k in ("summary", "descricao", "descrição", "description", "snippet", "detalhes", "content") if lower.get(k)), "")
            location = next((str(lower[k]) for k in ("location", "local", "cidade", "estado", "uf", "region", "regiao") if lower.get(k)), "Brasil / não especificado")
            # Se o objeto tiver aparência de concurso, registra-o; a elegibilidade será revisada depois.
            if title and (url or summary or any(k in lower for k in ("orgao", "órgão", "prazo", "inscricoes", "inscrições"))):
                candidates.append({"title": title, "url": url, "summary": summary, "location": location, "source": source, "search_term": search_term})
            for value in obj.values():
                if isinstance(value, (dict, list)):
                    walk(value)
        elif isinstance(obj, list):
            for value in obj:
                walk(value)

    if parsed is not None:
        walk(parsed)
    else:
        # Fallback para saída textual: cada URL é usada como âncora e o texto próximo como resumo.
        lines = [line.strip(" -*\t") for line in raw.splitlines() if line.strip()]
        url_re = re.compile(r"https?://[^\s)\]>]+", re.I)
        for i, line in enumerate(lines):
            match = url_re.search(line)
            if match:
                url = match.group(0).rstrip(".,")
                title = line.replace(url, "").strip(" -:|*") or (lines[i - 1] if i else search_term)
                context = " ".join(lines[max(0, i - 2):min(len(lines), i + 2)])[:1000]
                candidates.append({"title": title[:300], "url": url, "summary": context, "location": "Brasil / não especificado", "source": source, "search_term": search_term})
    # Deduplica itens repetidos dentro da mesma resposta.
    seen: set[str] = set()
    unique = []
    for c in candidates:
        key = normalize(c.get("url", "")) or normalize(c.get("title", ""))
        if key and key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def keyword_matches(candidate: dict[str, str], config: dict[str, Any]) -> list[str]:
    text = normalize(" ".join(candidate.get(k, "") for k in ("title", "summary", "location")))
    terms = list(config.get("areas_cargos", []))
    # Acrescenta palavras-chave amplas para encontrar cargos com nomes alternativos.
    terms += ["informática", "tecnologia da informação", "computação", "sistemas", "rede de computadores", "perito"]
    return [term for term in dict.fromkeys(terms) if normalize(term) and normalize(term) in text]


def assess_eligibility(candidate: dict[str, str], config: dict[str, Any]) -> tuple[str, str]:
    """Nunca declara elegibilidade confirmada sem texto explícito do requisito do edital."""
    text = normalize(" ".join(candidate.get(k, "") for k in ("title", "summary")))
    degree_terms = [normalize(x) for x in config["formacoes"].get("curso_principal", [])]
    if any(term and term in text for term in degree_terms):
        return "INDÍCIO: Ciência da Computação mencionada; confirmar no edital", "indicio"
    if config["formacoes"].get("qualquer_graduacao") and any(
        term in text for term in ("qualquer area de formacao", "graduacao em qualquer area", "nivel superior em qualquer area", "curso superior em qualquer area")
    ):
        return "INDÍCIO: parece aceitar qualquer graduação; confirmar no edital", "indicio"
    return "NÃO CONFIRMADO: abrir o edital e verificar formação e atribuições", "revisar_edital"


def fingerprint(candidate: dict[str, str]) -> str:
    identity = candidate.get("url", "").strip() or (candidate.get("title", "") + "|" + candidate.get("summary", ""))
    return hashlib.sha256(normalize(identity).encode("utf-8")).hexdigest()


def save_candidate(candidate: dict[str, str], matches: list[str], eligibility: str, status: str) -> bool:
    fp = fingerprint(candidate)
    with sqlite3.connect(DB_PATH) as con:
        cur = con.execute("""INSERT OR IGNORE INTO concursos
            (fingerprint,title,url,summary,location,source,first_seen_utc,notified,keyword_matches,eligibility,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (fp, candidate.get("title", "Sem título")[:500], candidate.get("url", ""), candidate.get("summary", "")[:4000],
             candidate.get("location", "Brasil / não especificado"), candidate.get("source", "PCI MCP"),
             datetime.now(timezone.utc).isoformat(), 0, json.dumps(matches, ensure_ascii=False), eligibility, status))
        con.commit()
        return cur.rowcount == 1


def mark_notified(candidate: dict[str, str]) -> None:
    with sqlite3.connect(DB_PATH) as con:
        con.execute("UPDATE concursos SET notified=1 WHERE fingerprint=?", (fingerprint(candidate),))
        con.commit()


def format_alert(candidate: dict[str, str], matches: list[str], eligibility: str) -> str:
    url = candidate.get("url") or "Link não retornado pela fonte; pesquise o título no site da PCI."
    summary = candidate.get("summary", "").strip()
    if len(summary) > 700:
        summary = summary[:697] + "..."
    match_text = ", ".join(matches[:8]) if matches else "correspondência ampla"
    return (
        "📢 POSSÍVEL CONCURSO COMPATÍVEL\n\n"
        f"📌 {candidate.get('title', 'Sem título')}\n"
        f"📍 Local: {candidate.get('location', 'Não informado')}\n"
        f"🔎 Palavras-chave: {match_text}\n"
        f"🎓 Formação: {eligibility}\n"
        f"🧾 Situação: a PCI MCP fornece principalmente resultados com inscrições abertas; confirme a situação atual no edital.\n"
        + (f"\nResumo: {summary}\n" if summary else "\n")
        + f"\n🔗 {url}\n\nNão considere a graduação elegível até conferir os requisitos do edital oficial."
    )


async def search_pci(config: dict[str, Any]) -> list[dict[str, str]]:
    all_candidates: list[dict[str, str]] = []
    async with streamable_http_client(PCI_MCP_URL) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools_result = await session.list_tools()
            tools = {tool.name: tool for tool in tools_result.tools}
            log.info("Ferramentas MCP disponíveis: %s", ", ".join(tools))
            preferred_names = ["buscar_por_cargo", "pesquisar_concursos", "listar_concursos"]
            selected = [tools[n] for n in preferred_names if n in tools]
            if not selected:
                raise RuntimeError("O servidor conectou, mas não expôs as ferramentas de busca esperadas. Consulte a lista de ferramentas impressa no log.")
            search_terms = config.get("areas_cargos", [])
            # Consultas por cargo e pesquisa livre. listar_concursos é chamado uma vez para cobrir a listagem geral.
            for tool in selected:
                terms = [""] if tool.name == "listar_concursos" else search_terms
                for term in terms:
                    try:
                        args = tool_search_argument(getattr(tool, "inputSchema", {}) or {}, term, config)
                        log.info("Consultando %s (termo=%s)", tool.name, term or "listagem geral")
                        result = await session.call_tool(tool.name, arguments=args)
                        all_candidates.extend(extract_candidates(result_to_text(result), "PCI Concursos MCP", term))
                    except Exception as exc:
                        log.warning("Falha na ferramenta %s para '%s': %s", tool.name, term, exc)
    # Dedup global por URL/título.
    unique: dict[str, dict[str, str]] = {}
    for c in all_candidates:
        key = normalize(c.get("url", "")) or normalize(c.get("title", ""))
        if key:
            unique.setdefault(key, c)
    return list(unique.values())


def run_cycle(config: dict[str, Any]) -> None:
    init_db()
    try:
        candidates = asyncio.run(search_pci(config))
    except Exception as exc:
        log.exception("A consulta à PCI falhou: %s", exc)
        telegram_send("⚠️ Radar de Concursos: não foi possível consultar a PCI Concursos nesta execução. Verifique o log do programa.")
        return

    log.info("Candidatos brutos recebidos: %d", len(candidates))
    new_count = 0
    for candidate in candidates:
        matches = keyword_matches(candidate, config)
        # O termo de busca pode localizar resultados com texto insuficiente; sem match textual, descarta.
        if not matches:
            continue
        eligibility, _ = assess_eligibility(candidate, config)
        # A PCI MCP documenta ferramentas para editais abertos; não rotular como autorizado/previsto.
        status = "inscricoes_abertas"
        is_new = save_candidate(candidate, matches, eligibility, status)
        if not is_new:
            continue
        new_count += 1
        if config.get("notificar_apenas_novos", True):
            if telegram_send(format_alert(candidate, matches, eligibility)):
                mark_notified(candidate)
    log.info("Novos candidatos notificados: %d", new_count)
    if "autorizado_ou_previsto" in config.get("situacoes_desejadas", []):
        log.warning("Concursos autorizados/previstos estão configurados como interesse, mas não são cobertos pelas ferramentas abertas da PCI MCP; essa categoria exige fonte adicional.")


def main() -> None:
    config = load_config()
    hours = max(1, int(config.get("frequencia_horas", 12)))
    log.info("Radar iniciado; intervalo de %s horas", hours)
    log.info("A busca por palavras-chave NÃO confirma elegibilidade acadêmica; o edital precisa ser verificado.")
    while True:
        run_cycle(config)
        log.info("Próxima consulta em %s horas", hours)
        time.sleep(hours * 3600)


if __name__ == "__main__":
    main()
