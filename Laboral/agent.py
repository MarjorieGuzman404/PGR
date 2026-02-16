from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.tools import FunctionTool


# -----------------------------
# Cargar .env
# -----------------------------
load_dotenv()
if not os.getenv("GOOGLE_API_KEY"):
    raise RuntimeError('Falta GOOGLE_API_KEY en .env (NO pegues keys en el código).')


# -----------------------------
# Data quemada (simula lo que viene del agente general)
# -----------------------------
HARDCODED_GENERAL_CASES: Dict[str, Dict[str, str]] = {
    "LAB-0001": {
        "relato": (
            "Trabajé como auxiliar de bodega. Me pagaban semanalmente. "
            "El empleador me dijo que ya no llegara y no me entregó carta de despido. "
            "Quedó pendiente el pago de la última semana."
        )
    },
    "LAB-0002": {
        "relato": (
            "Tuve un accidente en el trabajo y me dieron incapacidad. "
            "El empleador no quiso reconocer el incidente y me presionó para renunciar."
        )
    },
}


# -----------------------------
# Estados y modelo simple (en memoria)
# -----------------------------
class CaseState(str, Enum):
    RECEIVED = "RECEIVED"
    DETAILS_CAPTURED = "DETAILS_CAPTURED"
    DOCS_REQUESTED = "DOCS_REQUESTED"
    DOCS_RECEIVED = "DOCS_RECEIVED"
    VALIDATED = "VALIDATED"
    PRE_CLASSIFIED = "PRE_CLASSIFIED"
    EXPEDIENTE_GENERATED = "EXPEDIENTE_GENERATED"
    HANDOFF_TO_HUMAN = "HANDOFF_TO_HUMAN"


@dataclass
class DocumentChecklistItem:
    doc: str
    requerido: bool = False
    recibido: bool = False
    nota: Optional[str] = None


@dataclass
class CaseData:
    # Nota: TODO lo digita el OPERADOR (no el usuario final en el sistema)
    nombre: Optional[str] = None
    dui: Optional[str] = None
    contacto: Optional[str] = None

    empleador_nombre: Optional[str] = None
    cargo: Optional[str] = None
    salario_monto: Optional[str] = None
    salario_periodicidad: Optional[str] = None

    fecha_inicio: Optional[str] = None
    fecha_despido_ultimo_pago: Optional[str] = None

    # Relato base llega del “Agente General” (quemado) y operador puede agregar complemento
    relato: Optional[str] = None
    relato_extra: Optional[str] = None

    pretension: Optional[str] = None

    jurisdiccion_probable: Optional[str] = None
    banderas: List[str] = field(default_factory=list)


@dataclass
class CaseRecord:
    case_id: str
    state: CaseState = CaseState.RECEIVED
    created_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    updated_at: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")
    data: CaseData = field(default_factory=CaseData)
    checklist: Dict[str, DocumentChecklistItem] = field(default_factory=dict)
    expediente: Optional[str] = None


CASE_DB: Dict[str, CaseRecord] = {}


# -----------------------------
# Listas
# -----------------------------
OBLIGATORIOS_CAPTURA = [
    "Identidad básica (nombre, DUI, contacto)",
    "Empleador (nombre, empresa o persona)",
    "Cargo y salario (monto acordado y periodicidad)",
    "Fechas clave (inicio relación, fecha despido/último pago)",
    "Hechos (relato libre)",
    "Pretensión (qué busca: reinstalo, pago, constancia, etc.)",
]

DOCUMENTOS_BASE = [
    "Contrato (si existe)",
    "Constancia salarial / colillas / transferencias",
    "Carta de despido (si existe)",
    "Afiliación AFP/ISSS (si aplica)",
    "Comprobantes de pago",
    "Planillas / boletas / transferencias",
    "Comunicación con empleador (WhatsApp/email) si existe",
    "Constancia médica (si aplica)",
    "Incapacidad (si existe)",
    "Reportes (si existen)",
]

LABORAL_KEYWORDS = [
    "empleador", "salario", "pago", "despido", "jornada",
    "prestaciones", "contrato", "trabaj", "incapacidad", "accidente"
]
PENAL_SIGNALS = [
    "amenaza de muerte", "arma", "secuestro", "extorsión", "agresión sexual"
]


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def corresponde_a_laboral(relato_total: str) -> bool:
    t = _normalize(relato_total)
    if any(s in t for s in PENAL_SIGNALS):
        return False
    return any(k in t for k in LABORAL_KEYWORDS)


def route_operativa(relato_total: str) -> str:
    t = _normalize(relato_total)
    if any(s in t for s in PENAL_SIGNALS):
        return "PENAL (probable) — derivación sugerida, revisión humana obligatoria"
    if any(k in t for k in LABORAL_KEYWORDS):
        return "LABORAL (probable) — pre-clasificación operativa, revisión humana final"
    return "REVISAR (humano) — información insuficiente para ruta operativa"


# -----------------------------
# TOOLS (el operador llena todo)
# -----------------------------
def create_case(case_id: Optional[str] = None) -> dict:
    cid = (case_id or "LAB-0001").upper()
    if cid not in CASE_DB:
        CASE_DB[cid] = CaseRecord(case_id=cid)
    return {"status": "ok", "case_id": cid, "state": CASE_DB[cid].state.value}


def capture_identity(case_id: str, dui: str, nombre: str, contacto: str) -> dict:
    cid = case_id.upper()
    if cid not in CASE_DB:
        CASE_DB[cid] = CaseRecord(case_id=cid)
    c = CASE_DB[cid]
    c.data.dui = dui
    c.data.nombre = nombre
    c.data.contacto = contacto
    c.state = CaseState.DETAILS_CAPTURED
    return {"status": "ok", "case_id": cid, "state": c.state.value}


def pull_story_from_general(case_id: str) -> dict:
    cid = case_id.upper()
    if cid not in CASE_DB:
        CASE_DB[cid] = CaseRecord(case_id=cid)

    c = CASE_DB[cid]
    hard = HARDCODED_GENERAL_CASES.get(cid)
    if not hard:
        return {"status": "error", "error_message": "No hay relato quemado para ese case_id (usa LAB-0001 o LAB-0002)."}

    c.data.relato = hard["relato"]
    c.state = CaseState.DETAILS_CAPTURED

    return {
        "status": "ok",
        "case_id": cid,
        "formato": {
            "nombre_persona": c.data.nombre or "N/D",
            "dui": c.data.dui or "N/D",
            "relato": c.data.relato,
        },
        "state": c.state.value,
    }


def add_more_info(case_id: str, info_adicional: str) -> dict:
    cid = case_id.upper()
    c = CASE_DB.get(cid)
    if not c:
        return {"status": "error", "error_message": "case_id no encontrado."}
    c.data.relato_extra = info_adicional
    c.state = CaseState.DETAILS_CAPTURED
    return {"status": "ok", "case_id": cid, "state": c.state.value}


def capture_required_fields(
    case_id: str,
    empleador_nombre: str,
    cargo: str,
    salario_monto: str,
    salario_periodicidad: str,
    fecha_inicio: str,
    fecha_despido_ultimo_pago: str,
    pretension: str,
) -> dict:
    cid = case_id.upper()
    c = CASE_DB.get(cid)
    if not c:
        return {"status": "error", "error_message": "case_id no encontrado."}

    d = c.data
    d.empleador_nombre = empleador_nombre
    d.cargo = cargo
    d.salario_monto = salario_monto
    d.salario_periodicidad = salario_periodicidad
    d.fecha_inicio = fecha_inicio
    d.fecha_despido_ultimo_pago = fecha_despido_ultimo_pago
    d.pretension = pretension
    c.state = CaseState.DETAILS_CAPTURED
    return {"status": "ok", "case_id": cid, "state": c.state.value}


def request_documents(case_id: str) -> dict:
    cid = case_id.upper()
    c = CASE_DB.get(cid)
    if not c:
        return {"status": "error", "error_message": "case_id no encontrado."}

    c.checklist = {doc: DocumentChecklistItem(doc=doc, requerido=False, recibido=False) for doc in DOCUMENTOS_BASE}
    c.state = CaseState.DOCS_REQUESTED
    return {
        "status": "ok",
        "case_id": cid,
        "documentos_base": [{"doc": d.doc, "recibido": d.recibido} for d in c.checklist.values()],
        "state": c.state.value,
        "nota": "No son obligatorios, pero se solicitan si existen.",
    }


def mark_document(case_id: str, doc_name: str, existe: bool, nota: Optional[str] = None) -> dict:
    cid = case_id.upper()
    c = CASE_DB.get(cid)
    if not c:
        return {"status": "error", "error_message": "case_id no encontrado."}

    if not c.checklist:
        c.checklist = {doc: DocumentChecklistItem(doc=doc) for doc in DOCUMENTOS_BASE}

    item = c.checklist.get(doc_name) or DocumentChecklistItem(doc=doc_name)
    item.recibido = bool(existe)
    if nota:
        item.nota = nota
    c.checklist[doc_name] = item

    c.state = CaseState.DOCS_RECEIVED
    return {"status": "ok", "case_id": cid, "doc": doc_name, "existe": existe, "state": c.state.value}


def validate_and_recheck(case_id: str) -> dict:
    cid = case_id.upper()
    c = CASE_DB.get(cid)
    if not c:
        return {"status": "error", "error_message": "case_id no encontrado."}

    d = c.data
    flags: List[str] = []

    if not d.nombre: flags.append("Falta nombre.")
    if not d.dui: flags.append("Falta DUI.")
    if not d.contacto: flags.append("Falta contacto.")
    if not d.empleador_nombre: flags.append("Falta empleador (nombre).")
    if not d.cargo: flags.append("Falta cargo.")
    if not d.salario_monto or not d.salario_periodicidad: flags.append("Falta salario (monto/periodicidad).")
    if not d.fecha_inicio: flags.append("Falta fecha inicio relación.")
    if not d.fecha_despido_ultimo_pago: flags.append("Falta fecha despido/último pago.")
    if not d.relato: flags.append("Falta relato base (del agente general).")
    if not d.pretension: flags.append("Falta pretensión.")

    relato_total = (d.relato or "") + ("\n" + d.relato_extra if d.relato_extra else "")
    if any(s in _normalize(relato_total) for s in PENAL_SIGNALS):
        flags.append("Señales de posible materia penal (solo derivación sugerida).")

    d.banderas = flags
    c.state = CaseState.VALIDATED

    is_laboral = corresponde_a_laboral(relato_total)
    d.jurisdiccion_probable = route_operativa(relato_total)
    c.state = CaseState.PRE_CLASSIFIED

    return {
        "status": "ok",
        "case_id": cid,
        "flags": flags,
        "jurisdiccion_probable": d.jurisdiccion_probable,
        "corresponde_a_laboral_probable": is_laboral,
        "state": c.state.value,
    }


def generate_expediente(case_id: str) -> dict:
    cid = case_id.upper()
    c = CASE_DB.get(cid)
    if not c:
        return {"status": "error", "error_message": "case_id no encontrado."}

    d = c.data
    relato_total = (d.relato or "") + ("\n" + d.relato_extra if d.relato_extra else "")

    docs_lines = []
    for doc in DOCUMENTOS_BASE:
        item = c.checklist.get(doc) if c.checklist else None
        ok = (item.recibido if item else False)
        note = f" ({item.nota})" if (item and item.nota) else ""
        docs_lines.append(f"- {'✅' if ok else '▫️'} {doc}{note}")

    expediente = "\n".join(
        [
            "=== EXPEDIENTE (BORRADOR OPERATIVO) ===",
            f"Case ID: {cid}",
            "",
            "Formato solicitado:",
            f"Nombre persona: {d.nombre or 'N/D'}",
            f"DUI: {d.dui or 'N/D'}",
            "Relato de los hechos:",
            relato_total or "N/D",
            "",
            "Obligatorios capturados (digitados por operador):",
            f"- Contacto: {d.contacto or 'N/D'}",
            f"- Empleador: {d.empleador_nombre or 'N/D'}",
            f"- Cargo: {d.cargo or 'N/D'}",
            f"- Salario: {d.salario_monto or 'N/D'} ({d.salario_periodicidad or 'N/D'})",
            f"- Fechas: inicio={d.fecha_inicio or 'N/D'} | despido/último pago={d.fecha_despido_ultimo_pago or 'N/D'}",
            f"- Pretensión: {d.pretension or 'N/D'}",
            "",
            "Documentos base (si existen):",
            *docs_lines,
            "",
            "Pre-clasificación operativa (NO dictamen):",
            f"- Ruta/Jurisdicción probable: {d.jurisdiccion_probable or 'N/D'}",
            "",
            "Banderas / faltantes:",
            *(["- (Sin banderas)"] if not d.banderas else [f"- {x}" for x in d.banderas]),
            "",
            "Nota: revisión humana final obligatoria.",
        ]
    )

    c.expediente = expediente
    c.state = CaseState.EXPEDIENTE_GENERATED
    return {"status": "ok", "case_id": cid, "expediente": expediente, "state": c.state.value}


def handoff_to_human(case_id: str, nota: Optional[str] = None) -> dict:
    cid = case_id.upper()
    c = CASE_DB.get(cid)
    if not c:
        return {"status": "error", "error_message": "case_id no encontrado."}
    c.state = CaseState.HANDOFF_TO_HUMAN
    return {"status": "ok", "case_id": cid, "state": c.state.value, "nota": nota or ""}


# -----------------------------
# INSTRUCTION (enfoque correcto: el agente guía al OPERADOR)
# -----------------------------
INSTRUCTION = f"""
Eres el Agente Laboral para USO INTERNO.
Importante: la PERSONA usuaria NO escribe en el sistema. Quien escribe es el OPERADOR.
Tu trabajo es decirle al OPERADOR qué preguntarle a la persona y luego pedirle al OPERADOR que te pase las respuestas para registrarlas.

Reglas:
- Tono institucional, educado y claro.
- NO dar dictamen jurídico.
- Solo “pre-clasificación operativa” y siempre “revisión humana final”.

Flujo:

A) Inicio
1) Saluda y preséntate.
2) Pide al OPERADOR que indique el case_id (ej: LAB-0001 o LAB-0002).
   - Si no lo tiene, que elija uno de ejemplo o use create_case() para abrir uno.
   - Luego ejecuta pull_story_from_general(case_id).

B) Identidad y mostrar relato (formato requerido)
3) Indícale al OPERADOR que le pregunte a la persona:
   - “¿Me confirma su nombre completo?”
   - “¿Me dicta su DUI?”
   - “¿Me indica un número de contacto (teléfono o correo)?”
   Cuando el OPERADOR te dé esos 3 datos, guarda con capture_identity(case_id, dui, nombre, contacto).

4) Después, muestra en pantalla el formato:
   - Nombre persona
   - DUI
   - Relato de los hechos (del caso quemado)

C) Complemento del relato
5) Pregunta al OPERADOR:
   “¿La persona desea agregar más información al relato?”
   - Si SÍ: pide al OPERADOR que escriba el complemento tal cual (lo que la persona dice),
     guarda con add_more_info(case_id, info_adicional),
     y vuelve a mostrar el formato completo (nombre, DUI, relato + complemento).
   - Si NO: continuar.

D) Captura obligatoria (operador pregunta y digita)
6) Indícale al OPERADOR que pregunte y anote:
{chr(10).join([f"- {x}" for x in OBLIGATORIOS_CAPTURA])}
   Luego guarda con capture_required_fields(...).

E) Documentos (no obligatorios, pero pedir si existen)
7) Ejecuta request_documents(case_id).
8) Por cada documento en la lista, dile al OPERADOR que pregunte:
   “¿Cuenta con este documento o evidencia?”
   y que te responda Sí/No. Marca con mark_document(case_id, doc_name, existe).
Lista:
{chr(10).join([f"- {x}" for x in DOCUMENTOS_BASE])}

F) Validación y revisión operativa
9) Ejecuta validate_and_recheck(case_id) y explica el resultado al OPERADOR.
   - Si sale PENAL (probable): solo sugerir derivación y recalcar que es operativo (sin dictamen).

G) Expediente y cierre
10) Genera con generate_expediente(case_id) y muéstralo.
11) Cierra con handoff_to_human(case_id) indicando “Listo para revisión humana final”.
"""


# -----------------------------
# ROOT AGENT (ADK lo busca con este nombre)
# -----------------------------
root_agent = Agent(
    name="Laboral",
    model="gemini-2.5-flash",
    instruction=INSTRUCTION,
    tools=[
        FunctionTool(create_case),
        FunctionTool(capture_identity),
        FunctionTool(pull_story_from_general),
        FunctionTool(add_more_info),
        FunctionTool(capture_required_fields),
        FunctionTool(request_documents),
        FunctionTool(mark_document),
        FunctionTool(validate_and_recheck),
        FunctionTool(generate_expediente),
        FunctionTool(handoff_to_human),
    ],
)
