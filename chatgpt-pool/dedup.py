"""节点去重（§23）：按 node_id，绝不按节点名称。"""


def dedup(nodes):
    seen = set()
    out = []
    dupes = 0
    for n in nodes:
        if n.node_id in seen:
            dupes += 1
            continue
        seen.add(n.node_id)
        out.append(n)
    return out, dupes
