"""WhatsApp message templates (PII-free).

Templates contain static text with placeholders for non-PII params only.
Text is rendered only in-memory at send time, never persisted.
"""

from typing import Any

TEMPLATES: dict[str, dict[str, Any]] = {
    "prompt_dates": {
        "text": "Por favor, informe as datas de entrada e saída (ex: 10/02 a 12/02).",
        "allowed_params": [],
    },
    "prompt_room_type": {
        "text": "Qual tipo de quarto prefere? Temos Standard e Suíte disponíveis.",
        "allowed_params": [],
    },
    "prompt_adult_count": {
        "text": "Quantos adultos serão?",
        "allowed_params": [],
    },
    "prompt_child_count": {
        "text": "Quantas crianças estarão na hospedagem?",
        "allowed_params": [],
    },
    "prompt_children_ages": {
        "text": "Quais as idades das crianças? (ex.: 3 e 7)",
        "allowed_params": [],
    },
    "quote_unavailable": {
        "text": (
            "Infelizmente não temos disponibilidade para {checkin} a {checkout}. "
            "Gostaria de tentar outras datas?"
        ),
        "allowed_params": ["checkin", "checkout"],
    },
    "hold_unavailable": {
        "text": (
            "Ops! Parece que alguém acabou de reservar esse quarto. "
            "Gostaria de tentar outras datas?"
        ),
        "allowed_params": [],
    },
    "quote_available": {
        "text": (
            "Ótimo! Encontrei disponibilidade:\n"
            "- {nights} noite(s) de {checkin} a {checkout}\n"
            "- {adult_count} adulto(s)\n"
            "- Total: R$ {total_brl}\n\n"
            "Reserva válida por 15 minutos.\n"
            "Pague aqui: {checkout_url}"
        ),
        "allowed_params": [
            "nights",
            "checkin",
            "checkout",
            "adult_count",
            "total_brl",
            "checkout_url",
        ],
    },
    "quote_available_no_checkout": {
        "text": (
            "Ótimo! Encontrei disponibilidade:\n"
            "- {nights} noite(s) de {checkin} a {checkout}\n"
            "- {adult_count} adulto(s)\n"
            "- Total: R$ {total_brl}\n\n"
            "Reserva válida por 15 minutos. Deseja confirmar?"
        ),
        "allowed_params": [
            "nights",
            "checkin",
            "checkout",
            "adult_count",
            "total_brl",
        ],
    },
}


def render(template_key: str, params: dict[str, Any]) -> str:
    """Render template with params. Validates allowed_params.

    Args:
        template_key: Template identifier.
        params: Parameters to interpolate (must be in allowed_params).

    Returns:
        Rendered text string.

    Raises:
        ValueError: If template_key unknown or params contains disallowed keys.
    """
    if template_key not in TEMPLATES:
        raise ValueError(f"Unknown template: {template_key}")

    template = TEMPLATES[template_key]
    allowed = set(template["allowed_params"])
    provided = set(params.keys())

    # Validate no extra params (defense against PII leakage)
    extras = provided - allowed
    if extras:
        raise ValueError(f"Disallowed params for {template_key}: {extras}")

    return template["text"].format(**params)
