"""Add the post-augmentation final cleaning to describe_graph.py's description.json.

describe_graph.py describes the graph through the first cleaning (its "cleaned graph" basis).
Augmentation then added edges, and a final cleaning ran on the augmented graph. This records
that last step so the MCP server's build section reaches the deployed graph.
"""

import json
import sys
from pathlib import Path

description_in, first_report, final_report, description_out = map(Path, sys.argv[1:5])
d = json.loads(description_in.read_text())
first = json.loads(first_report.read_text())
final = json.loads(final_report.read_text())

added_edges = final["edges"]["in"] - first["edges"]["out"]
added_nodes = final["nodes"]["in"] - first["nodes"]["out"]
d["after_augmentation"] = {
    "note": (
        "Augmentation added literature-derived edges to the cleaned graph, then a final cleaning "
        "ran on the result. These are the deployed graph's totals; totals elsewhere in this build "
        "section describe the graph after the first cleaning, before augmentation."
    ),
    "added_by_augmentation": {"nodes": added_nodes, "edges": added_edges},
    "final_cleaning": {
        "nodes": final["nodes"],
        "edges": final["edges"],
        "dropped_edges": final.get("dropped_edges", {}),
        "dropped_nodes": final.get("dropped_nodes", {}),
        "flagged_edges": final.get("flagged_edges"),
        "flags": final.get("flags", {}),
    },
    "deployed_totals": {"nodes": final["nodes"]["out"], "edges": final["edges"]["out"]},
}
description_out.write_text(json.dumps(d, indent=2) + "\n")
a = d["after_augmentation"]
print("added by augmentation:", a["added_by_augmentation"])
print("final cleaning in/out:", final["nodes"], final["edges"])
print("deployed totals:", a["deployed_totals"], "flagged edges:", final.get("flagged_edges"))
