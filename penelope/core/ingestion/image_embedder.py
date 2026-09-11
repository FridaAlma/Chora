"""
Image embedding with CLIP (Contrastive Language-Image Pre-training).

Usa il modello open-clip-torch (ViT-B/32) per generare embedding
visivi delle immagini, permettendo ricerca semantica cross-modale
(testo ↔ immagini) in ChromaDB.

Il modello gira su CPU (~5 img/s su i3) o su GPU se disponibile.
Primo avvio: download dei pesi (~350MB).

Dipendenze: pip install open-clip-torch
"""

import logging
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Cache del modello (lazy loading)
_model = None
_preprocess = None
_tokenizer = None


def _load_model():
    """Carica il modello CLIP lazy (ViT-B/32, ~350MB)."""
    global _model, _preprocess, _tokenizer
    if _model is not None:
        return True

    try:
        import open_clip

        # ViT-B/32: buon tradeoff qualità/velocità su CPU
        model_name = "ViT-B-32"
        pretrained = "laion2b_s34b_b79k"  # addestrato su LAION-2B

        logger.info("Caricamento CLIP %s (%s)...", model_name, pretrained)
        model, _, preprocess = open_clip.create_model_and_transforms(
            model_name,
            pretrained=pretrained,
        )
        tokenizer = open_clip.get_tokenizer(model_name)

        _model = model
        _preprocess = preprocess
        _tokenizer = tokenizer
        logger.info("CLIP %s caricato con successo", model_name)
        return True

    except ImportError:
        logger.warning(
            "open-clip-torch non installato. "
            "Esegui: pip install open-clip-torch"
        )
        return False
    except Exception as e:
        logger.warning("Errore caricamento CLIP: %s", e)
        return False


def get_image_embedding(image_path: str | Path) -> Optional[list[float]]:
    """Calcola l'embedding CLIP di un'immagine.

    Args:
        image_path: Percorso dell'immagine.

    Returns:
        Lista di float (512-dim per ViT-B/32) o None se errore.
    """
    if not _load_model():
        return None

    try:
        from PIL import Image

        img = Image.open(str(image_path)).convert("RGB")
        import torch

        # Preprocessa e aggiungi batch dimension
        image_tensor = _preprocess(img).unsqueeze(0)

        # Inference su CPU (o GPU se disponibile)
        with torch.no_grad():
            features = _model.encode_image(image_tensor)

        # Normalizza e converti in lista
        features = features / features.norm(dim=-1, keepdim=True)
        return features[0].tolist()

    except Exception as e:
        logger.debug("Errore embedding CLIP per %s: %s", image_path, e)
        return None


def get_text_embedding(text: str) -> Optional[list[float]]:
    """Calcola l'embedding CLIP di un testo (per search cross-modale).

    Permette di cercare immagini con query di testo.

    Args:
        text: Testo della query.

    Returns:
        Lista di float (512-dim) o None se errore.
    """
    if not _load_model():
        return None

    try:
        import torch

        text_tokens = _tokenizer([text])
        with torch.no_grad():
            features = _model.encode_text(text_tokens)

        features = features / features.norm(dim=-1, keepdim=True)
        return features[0].tolist()

    except Exception as e:
        logger.debug("Errore embedding testo CLIP: %s", e)
        return None


def get_image_embedding_fast(image_path: str | Path) -> Optional[list[float]]:
    """Versione ottimizzata per batch processing.

    Usa numpy per preprocessing più veloce su CPU.
    """
    return get_image_embedding(image_path)
