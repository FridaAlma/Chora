"""
Test per Dispatcher — coda lazy di elaborazione.
"""

from unittest.mock import MagicMock, patch

import pytest

from core.ingestion.dispatcher import Dispatcher


@pytest.fixture
def db_mock():
    """Crea un MariaDBStore mock."""
    db = MagicMock()
    db.__enter__.return_value = db
    db.get_node.return_value = {
        "id": "node-1",
        "type": "File",
        "label": "test.txt",
        "metadata": None,
    }
    db._query.return_value = [{
        "path": "/tmp/test.txt",
        "node_id": "node-1",
    }]
    return db


@pytest.fixture
def queue_item():
    return {
        "id": 1,
        "node_id": "node-1",
        "status": "pending",
        "priority": 0,
    }


def test_dispatcher_init():
    """Dispatcher si inizializza senza errori."""
    d = Dispatcher()
    assert d is not None


def test_process_item_no_node(db_mock, queue_item):
    """Elemento senza nodo → mark_done con errore."""
    db_mock.get_node.return_value = None
    d = Dispatcher(db=db_mock)
    result = d.process_item(queue_item)
    assert not result
    db_mock.mark_done.assert_called_once()


def test_process_item_no_file_registry(db_mock, queue_item):
    """Elemento senza file_registry → mark_done con errore."""
    db_mock._query.return_value = []
    d = Dispatcher(db=db_mock)
    result = d.process_item(queue_item)
    assert not result
    db_mock.mark_done.assert_called_once()


def test_process_queue_empty(db_mock):
    """Coda vuota → 0 processati."""
    db_mock.dequeue.return_value = []
    d = Dispatcher(db=db_mock)
    count = d.process_queue(batch_size=5)
    assert count == 0


def test_process_queue_batch(db_mock):
    """Coda con elementi → processati."""
    items = [
        {"id": 1, "node_id": "n1", "status": "pending", "priority": 0},
        {"id": 2, "node_id": "n2", "status": "pending", "priority": 0},
    ]
    db_mock.dequeue.return_value = items

    d = Dispatcher(db=db_mock)
    count = d.process_queue(batch_size=5)
    assert count == 2  # due elementi processati


def test_run_loop_resets_stale(db_mock):
    """All'avvio del loop, gli elementi stale devono essere resettati."""
    db_mock.reset_stale_processing.return_value = 3

    d = Dispatcher(db=db_mock)
    # Ferma subito dopo il primo ciclo
    import threading
    timer = threading.Timer(0.1, d.stop)
    timer.start()
    d.run_loop(interval=0.05, batch_size=5)

    db_mock.reset_stale_processing.assert_called_once_with(max_age_minutes=5)


def test_stop():
    """Ferma il dispatcher."""
    d = Dispatcher()
    d.stop()
    assert not d._running
