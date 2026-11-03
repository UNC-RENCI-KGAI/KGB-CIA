# /// script
# requires-python = ">=3.12"
# dependencies = ["robokop-orion==2.0.13"]
# ///
"""Register a generated loader in-process and run ORION's native ingest pipeline."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("package", type=Path, help="Generated package root with registry.json")
    parser.add_argument("source", help="Generated ORION source ID from registry.json")
    parser.add_argument("workspace", type=Path, help="ORION storage directory under data/")
    parser.add_argument("--source-files", type=Path)
    parser.add_argument("--infores")
    parser.add_argument("--strict", action="store_true", help="Exclude unresolved nodes")
    parser.add_argument(
        "--conflation", action="store_true", help="Explicitly enable gene/protein conflation"
    )
    args = parser.parse_args(argv)
    root = args.package.resolve()
    args.workspace.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(root))
    if args.source_files:
        os.environ["KGB_CIA_SOURCE_FILES"] = str(args.source_files.resolve())
    os.environ["ORION_STORAGE"] = str(args.workspace.resolve())
    from orion import data_sources, ingest_pipeline
    from orion.normalization import NormalizationScheme

    entry = json.loads((root / "registry.json").read_text())[args.source]
    path = root / entry["parser"]
    name = "kgbcia_generated_loader"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    loader = getattr(module, entry["class"])
    config = json.loads(loader.mapping_file.read_text())
    if config.get("format_version", 1) < 2:
        parser.error("Regenerate this parser to install isolated support code and node merging")
    biolink_version = config.get("biolink_version")
    if not biolink_version:
        parser.error("Semantic mapping must declare its Biolink version before normalization")
    if args.infores:
        loader.provenance_id = args.infores
    if not loader.provenance_id.startswith("infores:"):
        parser.error("Supply --infores; this metadata bundle has no source identity")
    data_sources.SOURCE_DATA_LOADER_CLASS_IMPORTS[args.source] = (name, entry["class"])
    ingest_pipeline.SOURCE_DATA_LOADER_CLASSES.pop(args.source, None)
    # ORION expects built-in metadata paths relative to its installation. Supply this
    # generated source's absolute metadata path without editing the ORION installation.
    ingest_pipeline.get_data_source_metadata_path = lambda source: str(
        path.with_name(f"{source}.source.json")
    )
    source_metadata = json.loads(path.with_name(f"{args.source}.source.json").read_text())
    source_metadata["identifier"] = loader.provenance_id
    loader.get_source_meta_information = lambda self: dict(source_metadata)
    ingest_pipeline.IngestPipeline.load_parser_metadata = staticmethod(
        lambda source: dict(source_metadata)
    )
    with module.merging_node_writers():
        result = ingest_pipeline.IngestPipeline(
            storage_dir=str(args.workspace.resolve())
        ).run_pipeline(
            args.source,
            normalization_scheme=NormalizationScheme(
                strict=args.strict,
                conflation=args.conflation,
                edge_normalization_version=biolink_version,
            ),
        )
    return 0 if result else 1


if __name__ == "__main__":
    raise SystemExit(main())
