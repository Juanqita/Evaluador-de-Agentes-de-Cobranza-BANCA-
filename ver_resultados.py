"""
ver_resultados.py — Resumen legible de results.json: un renglón por
conversación con su veredicto, puntaje y banderas críticas.

Uso: python ver_resultados.py
"""
import json
from pathlib import Path

data = json.loads((Path(__file__).parent / "results.json").read_text(encoding="utf-8"))

print(f"{'ID':6} {'STATUS':22} {'VEREDICTO':26} {'PUNTAJE':8} BANDERAS")
print("-" * 90)
for r in data:
    ev = r.get("evaluation")
    if ev:
        print(f"{r['conversation_id']:6} {r['status']:22} {ev['veredicto']:26} "
              f"{ev['puntaje_total']:>5}   {', '.join(ev['banderas_criticas']) or '-'}")
    else:
        print(f"{r['conversation_id']:6} {r['status']:22} {'(sin evaluación)':26} "
              f"{'-':>5}   error: {(r.get('error') or '')[:60]}")

print()
print("Para ver el detalle regla por regla de UNA conversación, por ejemplo C05:")
print('  python -c "import json; d=json.load(open(\'results.json\',encoding=\'utf-8\')); '
      "print(json.dumps([r for r in d if r['conversation_id']=='C05'][0]['evaluation'], "
      'indent=2, ensure_ascii=False))"')
