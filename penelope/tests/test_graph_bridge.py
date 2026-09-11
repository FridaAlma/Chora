"""
Test per il bridge NetworkX ↔ MariaDB (con mock del DB).
"""

from unittest.mock import MagicMock, patch

from core.db.graph_bridge import GraphBridge


def fake_node(id_, type_="File", label="test", metadata=None, created_at="2025-01-01"):
    return {
        "id": id_,
        "type": type_,
        "label": label,
        "metadata": '{"key": "value"}' if metadata else None,
        "created_at": created_at,
    }


def fake_edge(id_, src, tgt, rel="MEMBER_OF", weight=1.0, metadata=None, created_at="2025-01-01"):
    return {
        "id": id_,
        "source_id": src,
        "target_id": tgt,
        "relation": rel,
        "weight": weight,
        "metadata": '{"note": "test"}' if metadata else None,
        "created_at": created_at,
    }


def _make_db():
    db = MagicMock()
    # __enter__ deve restituire self per far funzionare 'with db as store'
    db.__enter__.return_value = db
    return db


def test_load_graph():
    db = _make_db()
    db._query.side_effect = [
        [fake_node("n1", "File", "doc.txt"),
         fake_node("n2", "Project", "Progetto X")],
        [fake_edge(1, "n1", "n2")],
    ]

    bridge = GraphBridge(db)
    bridge.load_from_db()

    assert bridge.graph.number_of_nodes() == 2
    assert bridge.graph.number_of_edges() == 1
    assert bridge.graph.nodes["n1"]["type"] == "File"
    assert bridge.graph.nodes["n2"]["type"] == "Project"


def test_get_neighbors():
    db = _make_db()
    db._query.side_effect = [
        [fake_node("n1"), fake_node("n2"), fake_node("n3")],
        [fake_edge(1, "n1", "n2", "MEMBER_OF"),
         fake_edge(2, "n1", "n3", "APPEARS_IN")],
    ]

    bridge = GraphBridge(db)
    bridge.load_from_db()

    # Tutti i vicini di n1
    neigh = bridge.get_neighbors("n1")
    assert len(neigh) == 2

    # Solo MEMBER_OF
    neigh = bridge.get_neighbors("n1", relation="MEMBER_OF")
    assert len(neigh) == 1
    assert neigh[0]["node_id"] == "n2"


def test_shortest_path():
    db = _make_db()
    db._query.side_effect = [
        [fake_node("a"), fake_node("b"), fake_node("c")],
        [fake_edge(1, "a", "b"), fake_edge(2, "b", "c")],
    ]

    bridge = GraphBridge(db)
    bridge.load_from_db()

    path = bridge.shortest_path("a", "c")
    assert path == ["a", "b", "c"]

    path = bridge.shortest_path("a", "nonexistent")
    assert path is None


def test_merge_nodes():
    db = _make_db()
    db._query.side_effect = [
        [fake_node("keep"), fake_node("merge")],
        [fake_edge(1, "merge", "keep", "APPEARS_IN"),
         fake_edge(2, "keep", "merge", "MEMBER_OF")],
    ]
    db._execute = MagicMock(return_value=1)

    bridge = GraphBridge(db)
    bridge.load_from_db()

    assert bridge.graph.number_of_nodes() == 2
    bridge.merge_nodes("keep", "merge")
    assert bridge.graph.number_of_nodes() == 1
    # Gli archi devono essere stati riassegnati
    assert bridge.graph.number_of_edges() == 2
