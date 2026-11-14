"""Restricted visible-form operator for the official vacation system."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from pathlib import Path
import re
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from playwright.async_api import async_playwright


class BrowserError(Exception):
    def __init__(self, message: str, *, ambiguous: bool = False, evidence: str | None = None) -> None:
        super().__init__(message)
        self.ambiguous = bool(ambiguous)
        self.evidence = evidence


class VacationBrowser:
    """The application must validate review and exact human approval before calling."""

    def __init__(self, url: str, artifacts_dir: Path, headless: bool = True) -> None:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or parsed.path.rstrip("/") != "/legado/ferias":
            raise ValueError("A ferramenta só pode operar a tela /legado/ferias configurada.")
        self.url = url
        self.artifacts_dir = Path(artifacts_dir)
        self.headless = headless
        self._lock = asyncio.Lock()

    @staticmethod
    def _validated(data: dict[str, Any]) -> dict[str, str]:
        values = {key: str(data.get(key) or "").strip() for key in ("agente", "email", "inicio", "fim", "evento")}
        if not values["agente"]:
            raise BrowserError("Informe o nome do agente responsável pelo lançamento.")
        values["email"] = values["email"].lower()
        if not re.fullmatch(r"[a-z0-9._-]+@t4h\.exemplo", values["email"]):
            raise BrowserError("Férias exigem e-mail interno em @t4h.exemplo.")
        dates = {}
        for key in ("inicio", "fim"):
            if not re.fullmatch(r"\d{2}/\d{2}/\d{4}", values[key]):
                raise BrowserError("As datas devem usar DD/MM/AAAA.")
            try:
                dates[key] = datetime.strptime(values[key], "%d/%m/%Y").date()
            except ValueError as error:
                raise BrowserError("O período contém uma data inexistente.") from error
        if dates["fim"] < dates["inicio"]:
            raise BrowserError("O último dia não pode anteceder o primeiro.")
        if data.get("agora"):
            simulated = str(data["agora"])
            try:
                iso = re.search(r"\b\d{4}-\d{2}-\d{2}\b", simulated)
                local = re.search(r"\b\d{2}/\d{2}/\d{4}\b", simulated)
                if iso:
                    today = datetime.strptime(iso.group(0), "%Y-%m-%d").date()
                elif local:
                    today = datetime.strptime(local.group(0), "%d/%m/%Y").date()
                else:
                    raise ValueError("formato desconhecido")
            except ValueError as error:
                raise BrowserError("A data simulada informada é inválida.") from error
            if dates["inicio"] < today:
                raise BrowserError("As férias começam no passado do mundo simulado.")
        return values

    @staticmethod
    async def _matching_row(page: Any, values: dict[str, str]) -> str | None:
        for row in await page.locator("table tr").all():
            cells = [cell.strip() for cell in await row.locator("td").all_text_contents()]
            if len(cells) < 4:
                continue
            if cells[1].lower() == values["email"] and cells[2] == values["inicio"] and cells[3] == values["fim"]:
                match = re.fullmatch(r"FER-[A-Za-z0-9-]+", cells[0])
                if match:
                    return match.group(0)
        return None

    async def _evidence(self, page: Any) -> str | None:
        try:
            self.artifacts_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            path = (self.artifacts_dir / f"ferias-{stamp}-{uuid4().hex[:8]}.png").resolve()
            await page.screenshot(path=str(path), full_page=True)
            return str(path)
        except Exception:
            return None

    @staticmethod
    def _result(protocol: str, values: dict[str, str], evidence: str | None, *, submitted: bool) -> dict[str, Any]:
        return {"protocol": protocol, "protocolo": protocol, "email": values["email"],
                "inicio": values["inicio"], "fim": values["fim"], "evento": values["evento"],
                "agente": values["agente"], "evidence": evidence, "path": evidence,
                "submitted": submitted, "already_present": not submitted}

    async def submit(self, data: dict[str, Any]) -> dict[str, Any]:
        values = self._validated(data)
        async with self._lock:
            async with async_playwright() as playwright:
                browser = None
                page = None
                attempted = False
                try:
                    browser = await playwright.chromium.launch(headless=self.headless)
                    context = await browser.new_context(locale="pt-BR")
                    page = await context.new_page()
                    page.set_default_timeout(15000)
                    await page.goto(self.url, wait_until="domcontentloaded", timeout=30000)
                    existing = await self._matching_row(page, values)
                    if existing:
                        return self._result(existing, values, await self._evidence(page), submitted=False)
                    labels = {
                        "agente": "Quem está lançando (nome do agente)",
                        "email": "E-mail do colaborador",
                        "inicio": "Primeiro dia de férias (DD/MM/AAAA)",
                        "fim": "Último dia de férias (DD/MM/AAAA)",
                        "evento": "Evento relacionado (opcional)",
                    }
                    for key, label in labels.items():
                        await page.get_by_label(label, exact=True).fill(values[key])
                    attempted = True
                    await page.get_by_role("button", name="Lançar férias", exact=True).click()
                    await page.wait_for_load_state("domcontentloaded")
                    error_locator = page.locator(".erro")
                    if await error_locator.count():
                        message = (await error_locator.first.inner_text()).strip()
                        raise BrowserError(message or "A tela recusou o lançamento.", evidence=await self._evidence(page))
                    success = page.locator(".ok")
                    message = (await success.first.inner_text()).strip() if await success.count() else ""
                    match = re.search(r"\bFER-[A-Za-z0-9-]+\b", message)
                    in_table = await self._matching_row(page, values)
                    if match and in_table == match.group(0):
                        return self._result(in_table, values, await self._evidence(page), submitted=True)
                    raise BrowserError("A tela não confirmou o protocolo e o registro do período. Não repetir automaticamente.",
                                       ambiguous=True, evidence=await self._evidence(page))
                except asyncio.CancelledError:
                    raise
                except BrowserError:
                    raise
                except Exception as error:
                    # A click timeout may occur after the form reached the world. A fresh
                    # navigation only reads the visible table; it never resubmits the form.
                    if attempted and page is not None:
                        try:
                            await page.goto(self.url, wait_until="domcontentloaded", timeout=15000)
                            confirmed = await self._matching_row(page, values)
                            if confirmed:
                                result = self._result(confirmed, values, await self._evidence(page), submitted=True)
                                result["reconciled"] = True
                                return result
                        except Exception:
                            pass
                    evidence = await self._evidence(page) if page is not None else None
                    raise BrowserError("Não foi possível confirmar a operação na tela de férias. "
                                       + ("O envio pode ter ocorrido; conferir antes de repetir." if attempted else "Confira navegador e endereço configurados."),
                                       ambiguous=attempted, evidence=evidence) from error
                finally:
                    if browser is not None:
                        try:
                            await browser.close()
                        except Exception:
                            # A cleanup failure must not erase a confirmed submission.
                            pass
