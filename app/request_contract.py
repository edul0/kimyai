from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any


FEATURE_ALIASES = {
    "sidebar": ("sidebar", "menu lateral", "aside"),
    "busca": ("busca", "buscar", "pesquisa", "search"),
    "filtros": ("filtro", "filtrar", "filter"),
    "login": ("login", "entrar", "autenticacao", "sign in"),
    "cadastro": ("cadastro", "criar conta", "registrar", "sign up"),
    "recuperacao de senha": ("esqueci a senha", "recuperar senha", "redefinir senha", "forgot password"),
    "responsividade": ("responsivo", "mobile", "celular", "tablet"),
    "modo escuro": ("modo escuro", "tema escuro", "dark mode"),
    "dashboard": ("dashboard", "painel"),
    "graficos": ("grafico", "chart"),
    "exportacao": ("exportar", "download", "baixar"),
    "testes": ("teste", "testes", "pytest", "vitest", "jest"),
}


def normalize_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join("".join(char for char in decomposed if not unicodedata.combining(char)).lower().split())


def _unique(items: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = " ".join(item.strip().split()).strip(" .,:;\"'")
        key = normalize_text(cleaned)
        if cleaned and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


@dataclass
class RequestContract:
    request: str
    exact_terms: list[str] = field(default_factory=list)
    colors: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "request": self.request,
            "exact_terms": self.exact_terms,
            "colors": self.colors,
            "features": self.features,
            "constraints": self.constraints,
        }

    def as_prompt(self) -> str:
        lines = ["## Contrato verificavel do pedido"]
        if self.exact_terms:
            lines.append("- Textos/nomes literais: " + ", ".join(self.exact_terms))
        if self.colors:
            lines.append("- Cores literais: " + ", ".join(self.colors))
        if self.features:
            lines.append("- Capacidades obrigatorias: " + ", ".join(self.features))
        if self.constraints:
            lines.append("- Restricoes do usuario: " + " | ".join(self.constraints))
        lines.append("- Preserve todos estes itens durante refinamento, implementacao e autocorrecao.")
        return "\n".join(lines)


def build_request_contract(message: str) -> RequestContract:
    raw = " ".join(str(message or "").split()).strip()
    normalized = normalize_text(raw)
    quoted = re.findall(r'["“”\']([^"“”\']{2,80})["“”\']', raw)
    named = re.findall(
        r"\b(?:chamad[oa]|nome(?:ado)?|marca)\s+(?:de\s+)?([A-ZÀ-Ý][\wÀ-ÿ-]*(?:\s+[A-ZÀ-Ý][\wÀ-ÿ-]*){0,3})",
        raw,
    )
    colors = re.findall(r"#[0-9a-fA-F]{3,8}\b", raw)
    features = [
        canonical
        for canonical, aliases in FEATURE_ALIASES.items()
        if any(normalize_text(alias) in normalized for alias in aliases)
    ]
    constraints = re.findall(
        r"(?:^|[.;]\s*)([^.;]*(?:nao|nunca|sem|deve|precisa|obrigatorio|mantenha|preserve)[^.;]*)",
        normalized,
    )
    return RequestContract(
        request=raw[:1200],
        exact_terms=_unique(quoted + named)[:12],
        colors=_unique(colors)[:10],
        features=_unique(features)[:16],
        constraints=_unique(constraints)[:10],
    )


def evaluate_contract(contract: RequestContract | dict[str, Any], output: str) -> dict[str, Any]:
    data = contract.as_dict() if isinstance(contract, RequestContract) else dict(contract or {})
    haystack = normalize_text(output)
    checks: list[dict[str, Any]] = []
    for kind in ("exact_terms", "colors"):
        for value in data.get(kind) or []:
            checks.append({"kind": kind, "value": value, "met": normalize_text(value) in haystack})
    for feature in data.get("features") or []:
        aliases = FEATURE_ALIASES.get(feature, (feature,))
        checks.append({
            "kind": "feature",
            "value": feature,
            "met": any(normalize_text(alias) in haystack for alias in aliases),
        })
    met = sum(1 for item in checks if item["met"])
    score = 1.0 if not checks else met / len(checks)
    return {
        "score": round(score, 3),
        "met": met,
        "total": len(checks),
        "missing": [item["value"] for item in checks if not item["met"]],
        "checks": checks,
    }
