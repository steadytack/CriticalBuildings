"""One-off: add OS Open Map Local category names to the Rules sheet.

The original OS tags use the OS NGD 'Important Buildings' names (e.g. 'Primary School').
The free OS Open Map Local layer uses different names (e.g. 'Education > Primary Education').
New rows are added for the Open Map Local names; the original NGD rows are kept but set Active = N.
"""
import copy

import openpyxl
from openpyxl.styles import PatternFill

from rules_io import RULES_FILE

# rule ref -> Open Map Local 'Theme > Classification' values
OML = {
    1: ["Emergency Service > Fire Station"],
    2: ["Emergency Service > Police Station"],
    6: ["Medical Care > Hospital"],
    7: ["Medical Care > Hospice"],
    12: ["Medical Care > Medical Care Accommodation"],
    17: ["Education > Primary Education", "Education > Non State Primary Education"],
    18: ["Education > Secondary Education", "Education > Non State Secondary Education"],
    19: ["Education > Further Education", "Education > Higher or University Education"],
    21: ["Education > Special Needs Education"],
    22: ["Water Transport > Passenger Ferry Terminal",
         "Water Transport > Port Consisting of Docks and Nautical Berthing"],
    25: ["Air Transport > Airport"],
}
NOTES = {12: "Open Map Local 'Medical Care Accommodation' assumed to be care homes - please confirm.",
         22: "Open Map Local port polygons treated as ferry & ship terminals.",
         25: "Open Map Local airport building polygons."}

wb = openpyxl.load_workbook(RULES_FILE)
ws = wb["Rules"]
new_fill = PatternFill("solid", fgColor="E2F0D9")
grey = PatternFill("solid", fgColor="D9D9D9")

first_row = {}
for r in range(2, ws.max_row + 1):
    ref = ws.cell(r, 1).value
    if ref is not None:
        first_row.setdefault(int(ref), r)

# switch off original NGD OS rows
off = 0
for r in range(2, ws.max_row + 1):
    if ws.cell(r, 8).value == "OS" and ws.cell(r, 2).value == "Y":
        ws.cell(r, 2).value = "N"
        ws.cell(r, 10).value = "Original OS NGD name - not in the free OS Open Map Local data. Set to Y if you use OS NGD."
        for c in ws[r]:
            c.fill = grey
        off += 1

added = 0
for ref, tags in OML.items():
    base = first_row[ref]
    for tag in tags:
        ws.append([ref, "Y", ws.cell(base, 3).value, ws.cell(base, 4).value, ws.cell(base, 5).value,
                   ws.cell(base, 6).value, ws.cell(base, 7).value, "OS", tag, NOTES.get(ref, "OS Open Map Local")])
        for c in ws[ws.max_row]:
            c.fill = new_fill
        added += 1

cl = wb["Change log"]
cl.append(["All OS rules", f"{off} original OS NGD-name rows set Active=N (grey); {added} OS Open Map Local rows added (green)"])
cl.append(["OS source", "Free OS Open Map Local 'ImportantBuilding' layer is the OS data used; it has no categories for "
           "coastguard, lifeboat, rail stations, government buildings, conference centres - those come from OSM only"])
wb.save(RULES_FILE)
print(f"off={off} added={added}")
