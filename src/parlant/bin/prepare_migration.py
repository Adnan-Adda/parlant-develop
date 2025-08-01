# Copyright 2025 Emcie Co Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
from contextlib import AsyncExitStack, suppress
from datetime import datetime, timezone
import importlib
import json
import os
import shutil
from typing import Any, cast, Callable, Awaitable, Optional
import chromadb
from chromadb.api.types import IncludeEnum
from lagom import Container
from typing_extensions import NoReturn
from pathlib import Path
import sys
import rich
from rich.prompt import Confirm, Prompt

from parlant.adapters.db.json_file import JSONFileDocumentDatabase
from parlant.adapters.vector_db.chroma import ChromaDatabase
from parlant.core.common import generate_id, md5_checksum, Version
from parlant.core.context_variables import (
    ContextVariableDocument_v0_1_0,
    ContextVariableTagAssociationDocument,
    ContextVariableId,
)
from parlant.core.contextual_correlator import ContextualCorrelator
from parlant.core.evaluations import (
    EvaluationDocument,
    EvaluationDocument_v0_1_0,
    EvaluationId,
    EvaluationTagAssociationDocument,
    GuidelineContentDocument,
    GuidelinePayloadDocument,
    InvoiceDocument,
    InvoiceGuidelineDataDocument,
)
from parlant.core.glossary import (
    GlossaryVectorStore,
    TermDocument_v0_1_0,
    TermTagAssociationDocument,
    TermId,
)
from parlant.core.persistence.vector_database_helper import VectorDocumentStoreMigrationHelper
from parlant.core.journeys import (
    JourneyConditionAssociationDocument,
    JourneyDocument_v0_1_0,
    JourneyTagAssociationDocument,
    JourneyVectorStore,
)
from parlant.core.relationships import (
    GuidelineRelationshipDocument_v0_1_0,
    GuidelineRelationshipDocument_v0_2_0,
    RelationshipDocument,
)
from parlant.core.guidelines import (
    GuidelineDocument_v0_2_0,
    GuidelineTagAssociationDocument,
    GuidelineDocument,
    GuidelineId,
    guideline_document_converter_0_1_0_to_0_2_0,
    GuidelineDocument_v0_1_0,
)
from parlant.core.loggers import LogLevel, StdoutLogger
from parlant.core.nlp.embedding import EmbedderFactory
from parlant.core.persistence.common import ObjectId
from parlant.core.persistence.document_database import (
    BaseDocument,
    DocumentDatabase,
    identity_loader,
)
from parlant.core.persistence.document_database_helper import MetadataDocument
from parlant.core.tags import Tag
from parlant.core.utterances import (
    UtteranceTagAssociationDocument,
    UtteranceDocument_v0_1_0,
    UtteranceVectorStore,
)

DEFAULT_HOME_DIR = "runtime-data" if Path("runtime-data").exists() else "parlant-data"
PARLANT_HOME_DIR = Path(os.environ.get("PARLANT_HOME", DEFAULT_HOME_DIR))
PARLANT_HOME_DIR.mkdir(parents=True, exist_ok=True)

EXIT_STACK = AsyncExitStack()

sys.path.append(PARLANT_HOME_DIR.as_posix())
sys.path.append(".")

LOGGER = StdoutLogger(
    correlator=ContextualCorrelator(),
    log_level=LogLevel.INFO,
    logger_id="parlant.bin.prepare_migration",
)


class VersionCheckpoint:
    def __init__(self, component: str, from_version: str, to_version: str):
        self.component = component
        self.from_version = from_version
        self.to_version = to_version

    def __str__(self) -> str:
        return f"{self.component}: {self.from_version} -> {self.to_version}"


MigrationFunction = Callable[[], Awaitable[None]]
migration_registry: dict[tuple[str, str, str], MigrationFunction] = {}


def register_migration(
    component: str, from_version: str, to_version: str
) -> Callable[[MigrationFunction], MigrationFunction]:
    """Decorator to register migration functions"""

    def decorator(func: MigrationFunction) -> MigrationFunction:
        migration_registry[(component, from_version, to_version)] = func
        return func

    return decorator


async def get_component_versions() -> list[tuple[str, str]]:
    """Get current versions of all components"""
    versions = []

    def _get_version_from_json_file(
        file_path: Path,
        collection_name: str,
    ) -> Optional[str]:
        if not file_path.exists():
            return None

        with open(file_path, "r") as f:
            raw_data = json.load(f)
            if "metadata" in raw_data:
                return cast(str, raw_data["metadata"][0]["version"])
            else:
                items = raw_data.get(collection_name)
                if items and len(items) > 0:
                    return cast(str, items[0]["version"])
        return None

    agents_version = _get_version_from_json_file(
        PARLANT_HOME_DIR / "agents.json",
        "agents",
    )
    if agents_version:
        versions.append(("agents", agents_version))

    guidelines_version = _get_version_from_json_file(
        PARLANT_HOME_DIR / "guidelines.json",
        "guidelines",
    )
    if guidelines_version:
        versions.append(("guidelines", guidelines_version))

    context_vars_version = _get_version_from_json_file(
        PARLANT_HOME_DIR / "context_variables.json",
        "context_variables",
    )
    if context_vars_version:
        versions.append(("context_variables", context_vars_version))

    evaluations_version = _get_version_from_json_file(
        PARLANT_HOME_DIR / "evaluations.json",
        "evaluations",
    )
    if evaluations_version:
        versions.append(("evaluations", evaluations_version))

    guideline_connections_version = _get_version_from_json_file(
        PARLANT_HOME_DIR / "guideline_connections.json",
        "guideline_connections",
    )
    if guideline_connections_version:
        versions.append(("guideline_connections", guideline_connections_version))

    guideline_relationships_version = _get_version_from_json_file(
        PARLANT_HOME_DIR / "guideline_relationships.json",
        "guideline_relationships",
    )
    if guideline_relationships_version:
        versions.append(("guideline_relationships", guideline_relationships_version))

    embedder_factory = EmbedderFactory(Container())
    chroma_db = await EXIT_STACK.enter_async_context(
        ChromaDatabase(LOGGER, PARLANT_HOME_DIR, embedder_factory)
    )
    with suppress(chromadb.errors.InvalidCollectionException):
        if chroma_db.chroma_client.get_collection("glossary_unembedded"):
            chroma_db_metadata = cast(dict[str, Any], await chroma_db.read_metadata())

            versions.append(
                (
                    "glossary",
                    chroma_db_metadata.get(
                        VectorDocumentStoreMigrationHelper.get_store_version_key(
                            GlossaryVectorStore.__name__
                        ),
                        chroma_db_metadata.get(
                            "version", "0.1.0"
                        ),  # Back off to the old version key method if not found
                    ),
                )
            )

    utterances_version = _get_version_from_json_file(
        PARLANT_HOME_DIR / "utterances.json",
        "utterances",
    )
    if utterances_version:
        versions.append(("utterances", utterances_version))

    with suppress(chromadb.errors.InvalidCollectionException):
        if chroma_db.chroma_client.get_collection("utterances_unembedded"):
            chroma_db_metadata = cast(dict[str, Any], await chroma_db.read_metadata())

            versions.append(
                (
                    "utterances",
                    chroma_db_metadata.get(
                        VectorDocumentStoreMigrationHelper.get_store_version_key(
                            UtteranceVectorStore.__name__
                        ),
                        chroma_db_metadata.get(
                            "version", "0.1.0"
                        ),  # Back off to the old version key method if not found
                    ),
                )
            )

    journeys_version = _get_version_from_json_file(
        PARLANT_HOME_DIR / "journeys.json",
        "journeys",
    )
    if journeys_version:
        versions.append(("journeys", journeys_version))

    with suppress(chromadb.errors.InvalidCollectionException):
        if chroma_db.chroma_client.get_collection("journeys_unembedded"):
            chroma_db_metadata = cast(dict[str, Any], await chroma_db.read_metadata())

            versions.append(
                (
                    "journeys",
                    chroma_db_metadata.get(
                        VectorDocumentStoreMigrationHelper.get_store_version_key(
                            JourneyVectorStore.__name__
                        ),
                        chroma_db_metadata.get(
                            "version", "0.1.0"
                        ),  # Back off to the old version key method if not found
                    ),
                )
            )

    return versions


def backup_data() -> None:
    if Confirm.ask("Do you want to backup your data before migration?"):
        default_backup_dir = PARLANT_HOME_DIR.parent / "parlant-data.orig"
        try:
            backup_dir = Prompt.ask("Enter backup directory path", default=str(default_backup_dir))
            shutil.copytree(PARLANT_HOME_DIR, backup_dir, dirs_exist_ok=True)
            rich.print(f"[green]Data backed up to {backup_dir}")
        except Exception as e:
            rich.print(f"[red]Failed to backup data: {e}")
            die(f"Error backing up data: {e}")


async def create_metadata_collection(db: DocumentDatabase, collection_name: str) -> None:
    rich.print(f"[green]Migrating {collection_name} database...")
    try:
        collection = await db.get_collection(
            collection_name,
            BaseDocument,
            identity_loader,
        )

    except ValueError:
        rich.print(f"[yellow]Collection {collection_name} not found, skipping...")
        return

    try:
        metadata_collection = await db.get_collection(
            "metadata",
            BaseDocument,
            identity_loader,
        )
        await db.delete_collection("metadata")

    except ValueError:
        pass

    metadata_collection = await db.get_or_create_collection(
        "metadata",
        MetadataDocument,
        identity_loader,
    )

    if document := await collection.find_one({}):
        await metadata_collection.insert_one(
            {
                "id": ObjectId(generate_id()),
                "version": document["version"],
            }
        )
        rich.print(f"[green]Successfully migrated {collection_name} database")
    else:
        rich.print(f"[yellow]No documents found in {collection_name} collection.")


async def migrate_glossary_with_metadata() -> None:
    rich.print("[green]Starting glossary migration...")
    try:
        embedder_factory = EmbedderFactory(Container())

        db = await EXIT_STACK.enter_async_context(
            ChromaDatabase(LOGGER, PARLANT_HOME_DIR, embedder_factory)
        )

        try:
            old_collection = db.chroma_client.get_collection("glossary")
        except chromadb.errors.InvalidCollectionException:
            rich.print("[yellow]Glossary collection not found, skipping...")
            return

        if docs := old_collection.peek(limit=1)["metadatas"]:
            document = docs[0]

            version = document["version"]

            embedder_module = importlib.import_module(
                f"{old_collection.metadata['embedder_module_path']}_service"
            )
            embedder_type = getattr(
                embedder_module,
                old_collection.metadata["embedder_type_path"],
            )

            all_items = old_collection.get(
                include=[IncludeEnum.documents, IncludeEnum.embeddings, IncludeEnum.metadatas]
            )
            rich.print(f"[green]Found {len(all_items['ids'])} items to migrate")

            chroma_unembedded_collection = next(
                (
                    collection
                    for collection in db.chroma_client.list_collections()
                    if collection.name == "glossary_unembedded"
                ),
                None,
            ) or db.chroma_client.create_collection(name="glossary_unembedded")

            chroma_new_collection = next(
                (
                    collection
                    for collection in db.chroma_client.list_collections()
                    if collection.name == db.format_collection_name("glossary", embedder_type)
                ),
                None,
            ) or db.chroma_client.create_collection(
                name=db.format_collection_name("glossary", embedder_type)
            )

            if all_items["metadatas"] is None:
                rich.print("[yellow]No metadatas found in glossary collection, skipping...")
                return

            for i in range(len(all_items["metadatas"])):
                assert all_items["documents"] is not None
                assert all_items["embeddings"] is not None

                new_doc = {
                    **all_items["metadatas"][i],
                    "checksum": md5_checksum(all_items["documents"][i]),
                }

                chroma_unembedded_collection.add(
                    ids=[all_items["ids"][i]],
                    documents=[str(new_doc["content"])],
                    metadatas=[cast(chromadb.types.Metadata, new_doc)],
                    embeddings=[0],
                )

                chroma_new_collection.add(
                    ids=[all_items["ids"][i]],
                    documents=[str(new_doc["content"])],
                    metadatas=[cast(chromadb.types.Metadata, new_doc)],
                    embeddings=all_items["embeddings"][i],
                )

            # Version starts at 1
            chroma_unembedded_collection.modify(
                metadata={"version": 1 + len(all_items["metadatas"])}
            )
            chroma_new_collection.modify(metadata={"version": 1 + len(all_items["metadatas"])})

            await db.upsert_metadata(
                VectorDocumentStoreMigrationHelper.get_store_version_key(
                    GlossaryVectorStore.__name__
                ),
                version,
            )
            rich.print("[green]Successfully migrated glossary data")

        db.chroma_client.delete_collection(old_collection.name)
        rich.print("[green]Cleaned up old glossary collection")

    except Exception as e:
        rich.print(f"[red]Failed to migrate glossary: {e}")
        die(f"Error migrating glossary: {e}")


@register_migration("agents", "0.1.0", "0.2.0")
async def migrate_agents_0_1_0_to_0_2_0() -> None:
    rich.print("[green]Starting migration for agents 0.1.0 -> 0.2.0")

    agents_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "agents.json")
    )
    await create_metadata_collection(agents_db, "agents")

    context_variables_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "context_variables.json")
    )
    await create_metadata_collection(context_variables_db, "variables")

    tags_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "tags.json")
    )
    await create_metadata_collection(tags_db, "tags")

    customers_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "customers.json")
    )
    await create_metadata_collection(customers_db, "customers")

    sessions_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "sessions.json")
    )
    await create_metadata_collection(sessions_db, "sessions")

    guideline_tool_associations_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "guideline_tool_associations.json")
    )
    await create_metadata_collection(guideline_tool_associations_db, "associations")

    guidelines_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "guidelines.json")
    )
    await create_metadata_collection(guidelines_db, "guidelines")

    guideline_connections_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "guideline_connections.json")
    )
    await create_metadata_collection(guideline_connections_db, "guideline_connections")

    evaluations_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "evaluations.json")
    )
    await create_metadata_collection(evaluations_db, "evaluations")

    services_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "services.json")
    )
    await create_metadata_collection(services_db, "tool_services")

    await migrate_glossary_with_metadata()

    agent_collection = await agents_db.get_or_create_collection(
        "agents",
        BaseDocument,
        identity_loader,
    )

    for doc in await agent_collection.find(filters={}):
        await agent_collection.update_one(
            filters={"id": {"$eq": ObjectId(doc["id"])}},
            params={"version": Version.String("0.2.0")},
        )

    await upgrade_document_database_metadata(agents_db, Version.String("0.2.0"))


@register_migration("guidelines", "0.1.0", "0.3.0")
async def migrate_guidelines_0_1_0_to_0_3_0() -> None:
    async def _association_document_loader(
        doc: BaseDocument,
    ) -> Optional[GuidelineTagAssociationDocument]:
        return cast(GuidelineTagAssociationDocument, doc)

    rich.print("[green]Starting migration for guidelines 0.1.0 -> 0.3.0")
    guidelines_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "guidelines.json")
    )

    guideline_collection = await guidelines_db.get_or_create_collection(
        "guidelines",
        BaseDocument,
        identity_loader,
    )

    guideline_tags_collection = await guidelines_db.get_or_create_collection(
        "guideline_tag_associations",
        GuidelineTagAssociationDocument,
        _association_document_loader,
    )

    for guideline in await guideline_collection.find(filters={}):
        guideline_to_use = cast(GuidelineDocument_v0_2_0, guideline)
        if guideline["version"] == "0.1.0":
            converted_guideline = await guideline_document_converter_0_1_0_to_0_2_0(guideline)
            if not converted_guideline:
                rich.print(f"[red]Failed to migrate guideline {guideline['id']}")
                continue
            guideline_to_use = cast(GuidelineDocument_v0_2_0, converted_guideline)

        new_guideline = GuidelineDocument(
            id=guideline_to_use["id"],
            version=Version.String("0.3.0"),
            creation_utc=guideline_to_use["creation_utc"],
            condition=guideline_to_use["condition"],
            action=guideline_to_use["action"],
            enabled=guideline_to_use["enabled"],
        )

        await guideline_collection.delete_one(
            filters={"id": {"$eq": ObjectId(guideline["id"])}},
        )

        await guideline_collection.insert_one(new_guideline)

        await guideline_tags_collection.insert_one(
            {
                "id": ObjectId(generate_id()),
                "version": Version.String("0.3.0"),
                "creation_utc": datetime.now(timezone.utc).isoformat(),
                "guideline_id": GuidelineId(guideline["id"]),
                "tag_id": Tag.for_agent_id(
                    cast(GuidelineDocument_v0_1_0, guideline)["guideline_set"]
                ),
            }
        )

    await upgrade_document_database_metadata(guidelines_db, Version.String("0.3.0"))

    rich.print("[green]Successfully migrated guidelines to 0.3.0")


@register_migration("context_variables", "0.1.0", "0.2.0")
async def migrate_context_variables_0_1_0_to_0_2_0() -> None:
    async def _association_document_loader(
        doc: BaseDocument,
    ) -> Optional[ContextVariableTagAssociationDocument]:
        return cast(ContextVariableTagAssociationDocument, doc)

    context_variables_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "context_variables.json")
    )

    context_variables_collection = await context_variables_db.get_or_create_collection(
        "variables",
        BaseDocument,
        identity_loader,
    )
    context_variable_tags_collection = await context_variables_db.get_or_create_collection(
        "variable_tag_associations",
        ContextVariableTagAssociationDocument,
        _association_document_loader,
    )

    for context_variable in await context_variables_collection.find(filters={}):
        await context_variable_tags_collection.insert_one(
            {
                "id": ObjectId(generate_id()),
                "version": Version.String("0.2.0"),
                "creation_utc": datetime.now(timezone.utc).isoformat(),
                "variable_id": ContextVariableId(context_variable["id"]),
                "tag_id": Tag.for_agent_id(
                    cast(ContextVariableDocument_v0_1_0, context_variable)["variable_set"]
                ),
            }
        )

        await context_variables_collection.update_one(
            filters={"id": {"$eq": ObjectId(context_variable["id"])}},
            params={"version": Version.String("0.2.0")},
        )

    context_variable_values_collection = await context_variables_db.get_or_create_collection(
        "context_variable_values",
        BaseDocument,
        identity_loader,
    )

    for value in await context_variable_values_collection.find(filters={}):
        await context_variable_values_collection.update_one(
            filters={"id": {"$eq": ObjectId(value["id"])}},
            params={"version": Version.String("0.2.0")},
        )

    await upgrade_document_database_metadata(context_variables_db, Version.String("0.2.0"))

    rich.print("[green]Successfully migrated context variables to 0.2.0")


@register_migration("agents", "0.2.0", "0.3.0")
async def migrate_agents_0_2_0_to_0_3_0() -> None:
    agent_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "agents.json")
    )

    agent_collection = await agent_db.get_or_create_collection(
        "agents",
        BaseDocument,
        identity_loader,
    )

    await agent_db.get_or_create_collection(
        "agent_tags",
        BaseDocument,
        identity_loader,
    )

    for agent in await agent_collection.find(filters={}):
        if agent["version"] == "0.2.0":
            await agent_collection.update_one(
                filters={"id": {"$eq": ObjectId(agent["id"])}},
                params={"version": Version.String("0.3.0")},
            )

    await upgrade_document_database_metadata(agent_db, Version.String("0.3.0"))

    rich.print("[green]Successfully migrated agents from 0.2.0 to 0.3.0")


@register_migration("glossary", "0.1.0", "0.2.0")
async def migrate_glossary_0_1_0_to_0_2_0() -> None:
    rich.print("[green]Starting migration for glossary 0.1.0 -> 0.2.0")

    async def _association_document_loader(
        doc: BaseDocument,
    ) -> Optional[TermTagAssociationDocument]:
        return cast(TermTagAssociationDocument, doc)

    embedder_factory = EmbedderFactory(Container())

    db = await EXIT_STACK.enter_async_context(
        ChromaDatabase(LOGGER, PARLANT_HOME_DIR, embedder_factory)
    )

    glossary_tags_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "glossary_tags.json")
    )

    glossary_tags_collection = await glossary_tags_db.get_or_create_collection(
        "glossary_tags",
        TermTagAssociationDocument,
        _association_document_loader,
    )

    chroma_unembedded_collection = next(
        (
            collection
            for collection in db.chroma_client.list_collections()
            if collection.name == "glossary_unembedded"
        ),
        None,
    ) or db.chroma_client.create_collection(name="glossary_unembedded")

    migrated_count = 0
    if metadatas := chroma_unembedded_collection.get()["metadatas"]:
        for doc in metadatas:
            new_doc = {
                "id": doc["id"],
                "version": Version.String("0.2.0"),
                "checksum": md5_checksum(
                    cast(str, doc["content"]) + datetime.now(timezone.utc).isoformat()
                ),
                "content": doc["content"],
                "creation_utc": doc["creation_utc"],
                "name": doc["name"],
                "description": doc["description"],
                "synonyms": doc["synonyms"],
            }

            chroma_unembedded_collection.delete(
                where=cast(chromadb.Where, {"id": {"$eq": cast(str, doc["id"])}})
            )
            chroma_unembedded_collection.add(
                ids=[cast(str, doc["id"])],
                documents=[cast(str, doc["content"])],
                metadatas=[cast(chromadb.Metadata, new_doc)],
                embeddings=[0],
            )
            migrated_count += 1

            migrated_count += 1

            await glossary_tags_collection.insert_one(
                {
                    "id": ObjectId(generate_id()),
                    "version": Version.String("0.2.0"),
                    "creation_utc": datetime.now(timezone.utc).isoformat(),
                    "term_id": TermId(cast(str, doc["id"])),
                    "tag_id": Tag.for_agent_id(cast(TermDocument_v0_1_0, doc)["term_set"]),
                }
            )

    chroma_unembedded_collection.modify(metadata={"version": 1 + migrated_count})

    await db.upsert_metadata(
        VectorDocumentStoreMigrationHelper.get_store_version_key(GlossaryVectorStore.__name__),
        Version.String("0.2.0"),
    )
    await upgrade_document_database_metadata(glossary_tags_db, Version.String("0.2.0"))

    rich.print("[green]Successfully migrated glossary from 0.1.0 to 0.2.0")


@register_migration("utterances", "0.1.0", "0.2.0")
async def migrate_utterances_0_1_0_to_0_2_0() -> None:
    rich.print("[green]Starting migration for utterances 0.1.0 -> 0.2.0")

    async def _association_document_loader(
        doc: BaseDocument,
    ) -> Optional[UtteranceTagAssociationDocument]:
        return cast(UtteranceTagAssociationDocument, doc)

    utterances_json_file = PARLANT_HOME_DIR / "utterances.json"

    embedder_factory = EmbedderFactory(Container())

    utterances_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(
            LOGGER,
            utterances_json_file,
        )
    )

    utterances_collection = await utterances_db.get_or_create_collection(
        "utterances",
        BaseDocument,
        identity_loader,
    )

    utterance_tags_collection = await utterances_db.get_or_create_collection(
        "utterance_tag_associations",
        UtteranceTagAssociationDocument,
        _association_document_loader,
    )

    db = await EXIT_STACK.enter_async_context(
        ChromaDatabase(LOGGER, PARLANT_HOME_DIR, embedder_factory)
    )

    utterance_tags_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "utterance_tags.json")
    )

    chroma_unembedded_collection = next(
        (
            collection
            for collection in db.chroma_client.list_collections()
            if collection.name == "utterances_unembedded"
        ),
        None,
    ) or db.chroma_client.create_collection(name="utterances_unembedded")

    new_utterance_tags_collection = await utterance_tags_db.get_or_create_collection(
        "utterance_tags",
        UtteranceTagAssociationDocument,
        _association_document_loader,
    )

    migrated_count = 0
    for doc in await utterances_collection.find(filters={}):
        if doc["version"] == "0.1.0":
            doc = cast(UtteranceDocument_v0_1_0, doc)

            content = doc["value"]

            new_doc = {
                "id": doc["id"],
                "version": Version.String("0.2.0"),
                "content": content,
                "checksum": md5_checksum(content),
                "creation_utc": doc["creation_utc"],
                "value": doc["value"],
                "fields": json.dumps(doc["fields"]),
            }

            chroma_unembedded_collection.add(
                ids=[str(doc["id"])],
                documents=[content],
                metadatas=[cast(chromadb.Metadata, new_doc)],
                embeddings=[0],
            )

            migrated_count += 1

    for tag_doc in await utterance_tags_collection.find(filters={}):
        await new_utterance_tags_collection.insert_one(
            {
                "id": tag_doc["id"],
                "version": Version.String("0.2.0"),
                "creation_utc": tag_doc["creation_utc"],
                "utterance_id": tag_doc["utterance_id"],
                "tag_id": tag_doc["tag_id"],
            }
        )

    chroma_unembedded_collection.modify(metadata={"version": 1 + migrated_count})

    await db.upsert_metadata(
        VectorDocumentStoreMigrationHelper.get_store_version_key(UtteranceVectorStore.__name__),
        Version.String("0.2.0"),
    )
    await upgrade_document_database_metadata(utterance_tags_db, Version.String("0.2.0"))

    utterances_json_file.unlink()

    rich.print("[green]Successfully migrated utterances from 0.1.0 to 0.2.0")


@register_migration("journeys", "0.1.0", "0.2.0")
async def migrate_journeys_0_1_0_to_0_2_0() -> None:
    rich.print("[green]Starting migration for journeys 0.1.0 -> 0.2.0")

    async def _tag_association_document_loader(
        doc: BaseDocument,
    ) -> Optional[JourneyTagAssociationDocument]:
        return cast(JourneyTagAssociationDocument, doc)

    async def _condition_association_document_loader(
        doc: BaseDocument,
    ) -> Optional[JourneyConditionAssociationDocument]:
        return cast(JourneyConditionAssociationDocument, doc)

    journeys_json_file = PARLANT_HOME_DIR / "journeys.json"

    embedder_factory = EmbedderFactory(Container())

    journeys_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(
            LOGGER,
            journeys_json_file,
        )
    )

    journeys_collection = await journeys_db.get_or_create_collection(
        "journeys",
        BaseDocument,
        identity_loader,
    )

    journey_tags_collection = await journeys_db.get_or_create_collection(
        "journey_tag_associations",
        JourneyTagAssociationDocument,
        _tag_association_document_loader,
    )

    journey_conditions_collection = await journeys_db.get_or_create_collection(
        "journey_condition_associations",
        JourneyConditionAssociationDocument,
        _condition_association_document_loader,
    )

    db = await EXIT_STACK.enter_async_context(
        ChromaDatabase(LOGGER, PARLANT_HOME_DIR, embedder_factory)
    )

    journey_associations_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "journey_associations.json")
    )

    chroma_unembedded_collection = next(
        (
            collection
            for collection in db.chroma_client.list_collections()
            if collection.name == "journeys_unembedded"
        ),
        None,
    ) or db.chroma_client.create_collection(name="journeys_unembedded")

    new_journey_tags_collection = await journey_associations_db.get_or_create_collection(
        "journey_tags",
        JourneyTagAssociationDocument,
        _tag_association_document_loader,
    )

    new_journey_conditions_collection = await journey_associations_db.get_or_create_collection(
        "journey_conditions",
        JourneyConditionAssociationDocument,
        _condition_association_document_loader,
    )

    migrated_count = 0
    for doc in await journeys_collection.find(filters={}):
        if doc["version"] == "0.1.0":
            doc = cast(JourneyDocument_v0_1_0, doc)

            conditions = [
                c["condition"]
                for c in await journey_conditions_collection.find(
                    filters={"journey_id": {"$eq": ObjectId(doc["id"])}}
                )
            ]

            content = JourneyVectorStore.assemble_content(
                title=doc["title"],
                description=doc["description"],
                conditions=conditions,
            )

            new_doc = {
                "id": doc["id"],
                "version": Version.String("0.2.0"),
                "content": content,
                "checksum": md5_checksum(content),
                "creation_utc": doc["creation_utc"],
                "title": doc["title"],
                "description": doc["description"],
            }

            chroma_unembedded_collection.add(
                ids=[str(doc["id"])],
                documents=[content],
                metadatas=[cast(chromadb.Metadata, new_doc)],
                embeddings=[0],
            )

            migrated_count += 1

    for tag_doc in await journey_tags_collection.find(filters={}):
        await new_journey_tags_collection.insert_one(
            {
                "id": tag_doc["id"],
                "version": Version.String("0.2.0"),
                "creation_utc": tag_doc["creation_utc"],
                "journey_id": tag_doc["journey_id"],
                "tag_id": tag_doc["tag_id"],
            }
        )

    for condition_doc in await journey_conditions_collection.find(filters={}):
        await new_journey_conditions_collection.insert_one(
            {
                "id": condition_doc["id"],
                "version": Version.String("0.2.0"),
                "creation_utc": condition_doc["creation_utc"],
                "journey_id": condition_doc["journey_id"],
                "condition": condition_doc["condition"],
            }
        )

    chroma_unembedded_collection.modify(metadata={"version": 1 + migrated_count})

    await db.upsert_metadata("version", Version.String("0.2.0"))
    await upgrade_document_database_metadata(journey_associations_db, Version.String("0.2.0"))

    journeys_json_file.unlink()

    rich.print("[green]Successfully migrated journeys from 0.1.0 to 0.2.0")


@register_migration("evaluations", "0.1.0", "0.2.0")
async def migrate_evaluations_0_1_0_to_0_2_0() -> None:
    async def _association_document_loader(
        doc: BaseDocument,
    ) -> Optional[EvaluationTagAssociationDocument]:
        return cast(EvaluationTagAssociationDocument, doc)

    rich.print("[green]Starting migration for evaluations 0.1.0 -> 0.2.0")
    evaluations_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "evaluations.json")
    )

    evaluation_collection = await evaluations_db.get_or_create_collection(
        "evaluations",
        BaseDocument,
        identity_loader,
    )

    evaluation_tag_associations_collection = await evaluations_db.get_or_create_collection(
        "evaluation_tag_associations",
        EvaluationTagAssociationDocument,
        _association_document_loader,
    )

    for doc in await evaluation_collection.find(filters={}):
        if doc["version"] == "0.1.0":
            evaluation_doc = cast(EvaluationDocument_v0_1_0, doc)

            new_evaluation = EvaluationDocument(
                id=evaluation_doc["id"],
                version=Version.String("0.2.0"),
                creation_utc=evaluation_doc["creation_utc"],
                status=evaluation_doc["status"],
                error=evaluation_doc["error"],
                invoices=[
                    InvoiceDocument(
                        kind=i["kind"],
                        payload=GuidelinePayloadDocument(
                            content=GuidelineContentDocument(
                                condition=i["payload"]["content"]["condition"],
                                action=i["payload"]["content"]["action"],
                            ),
                            tool_ids=[],
                            action=i["payload"]["action"],
                            updated_id=i["payload"]["updated_id"],
                            coherence_check=i["payload"]["coherence_check"],
                            connection_proposition=i["payload"]["connection_proposition"],
                            action_proposition=False,
                            properties_proposition=False,
                        ),
                        checksum=i["checksum"],
                        state_version=i["state_version"],
                        approved=i["approved"],
                        data=InvoiceGuidelineDataDocument(
                            coherence_checks=i["data"]["coherence_checks"],
                            connection_propositions=i["data"]["connection_propositions"],
                            action_proposition=None,
                            properties_proposition=None,
                        )
                        if i["data"] is not None
                        else None,
                        error=None,
                    )
                    for i in evaluation_doc["invoices"]
                ],
                progress=evaluation_doc["progress"],
            )

            await evaluation_collection.delete_one(
                filters={"id": {"$eq": ObjectId(evaluation_doc["id"])}},
            )

            await evaluation_collection.insert_one(new_evaluation)

            await evaluation_tag_associations_collection.insert_one(
                {
                    "id": ObjectId(generate_id()),
                    "version": Version.String("0.2.0"),
                    "creation_utc": datetime.now(timezone.utc).isoformat(),
                    "evaluation_id": EvaluationId(evaluation_doc["id"]),
                    "tag_id": Tag.for_agent_id(evaluation_doc["agent_id"]),
                }
            )

    await upgrade_document_database_metadata(evaluations_db, Version.String("0.2.0"))

    rich.print("[green]Successfully migrated evaluations from 0.1.0 to 0.2.0")


async def upgrade_document_database_metadata(
    db: DocumentDatabase,
    to_version: Version.String,
) -> None:
    metadata_collection = await db.get_or_create_collection(
        "metadata",
        BaseDocument,
        identity_loader,
    )

    if metadata_document := await metadata_collection.find_one(filters={}):
        await metadata_collection.update_one(
            filters={"id": {"$eq": metadata_document["id"]}},
            params={"version": to_version},
        )
    else:
        await metadata_collection.insert_one(
            {
                "id": ObjectId(generate_id()),
                "version": to_version,
            }
        )


@register_migration("guideline_connections", "0.1.0", "0.2.0")
async def migrate_guideline_relationships_0_1_0_to_0_2_0() -> None:
    rich.print("[green]Starting migration for guideline relationships 0.1.0 -> 0.2.0")

    guideline_relationships_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "guideline_relationships.json")
    )

    guideline_relationships_collection = await guideline_relationships_db.get_or_create_collection(
        "guideline_relationships",
        BaseDocument,
        identity_loader,
    )

    relationships_metadata_collection = await guideline_relationships_db.get_or_create_collection(
        "metadata",
        MetadataDocument,
        identity_loader,
    )

    async with JSONFileDocumentDatabase(
        LOGGER, PARLANT_HOME_DIR / "guideline_connections.json"
    ) as guideline_connections_db:
        guideline_connections_collection = await guideline_connections_db.get_or_create_collection(
            "guideline_connections",
            BaseDocument,
            identity_loader,
        )

        for doc in await guideline_connections_collection.find(filters={}):
            doc = cast(GuidelineRelationshipDocument_v0_1_0, doc)
            await guideline_relationships_collection.insert_one(
                cast(
                    RelationshipDocument,
                    {
                        "id": doc["id"],
                        "version": Version.String("0.2.0"),
                        "creation_utc": doc["creation_utc"],
                        "source": doc["source"],
                        "target": doc["target"],
                        "kind": "entailment",
                    },
                )
            )

        connections_metadata_collection = await guideline_connections_db.get_or_create_collection(
            "metadata",
            MetadataDocument,
            identity_loader,
        )

        if metadata_doc := await connections_metadata_collection.find_one(filters={}):
            await relationships_metadata_collection.insert_one(
                cast(
                    MetadataDocument,
                    {
                        "id": metadata_doc["id"],
                        "version": Version.String("0.2.0"),
                    },
                )
            )

    (PARLANT_HOME_DIR / "guideline_connections.json").unlink()

    rich.print("[green]Successfully migrated guideline connections to guideline relationships")


@register_migration("guideline_relationships", "0.2.0", "0.3.0")
async def migrate_relationships_0_2_0_to_0_3_0() -> None:
    rich.print("[green]Starting migration for relationships 0.2.0 -> 0.3.0")

    relationships_db = await EXIT_STACK.enter_async_context(
        JSONFileDocumentDatabase(LOGGER, PARLANT_HOME_DIR / "relationships.json")
    )

    relationships_collection = await relationships_db.get_or_create_collection(
        "relationships",
        BaseDocument,
        identity_loader,
    )

    relationships_metadata_collection = await relationships_db.get_or_create_collection(
        "metadata",
        MetadataDocument,
        identity_loader,
    )

    async with JSONFileDocumentDatabase(
        LOGGER, PARLANT_HOME_DIR / "guideline_relationships.json"
    ) as guideline_relationships_db:
        guideline_relationships_collection = (
            await guideline_relationships_db.get_or_create_collection(
                "guideline_relationships",
                BaseDocument,
                identity_loader,
            )
        )

        for doc in await guideline_relationships_collection.find(filters={}):
            doc = cast(GuidelineRelationshipDocument_v0_2_0, doc)
            await relationships_collection.insert_one(
                cast(
                    RelationshipDocument,
                    {
                        "id": doc["id"],
                        "version": Version.String("0.3.0"),
                        "creation_utc": doc["creation_utc"],
                        "source": doc["source"],
                        "source_type": "guideline",
                        "target": doc["target"],
                        "target_type": "guideline",
                        "kind": doc["kind"],
                    },
                )
            )

        guideline_relationships_metadata_collection = (
            await guideline_relationships_db.get_or_create_collection(
                "metadata",
                MetadataDocument,
                identity_loader,
            )
        )

        if metadata_doc := await guideline_relationships_metadata_collection.find_one(filters={}):
            await relationships_metadata_collection.insert_one(
                cast(
                    MetadataDocument,
                    {
                        "id": metadata_doc["id"],
                        "version": Version.String("0.3.0"),
                    },
                )
            )

    (PARLANT_HOME_DIR / "guideline_relationships.json").unlink()

    rich.print("[green]Successfully migrated guideline connections to guideline relationships")


async def detect_required_migrations() -> list[tuple[str, str, str]]:
    component_versions = await get_component_versions()
    required_migrations = []

    for component, current_version in component_versions:
        applicable_migrations = []
        for key in migration_registry:
            migration_component, from_version, to_version = key
            if migration_component == component:
                if current_version == from_version:
                    applicable_migrations.append(key)
                elif Version.from_string(current_version) > Version.from_string(
                    from_version
                ) and Version.from_string(current_version) < Version.from_string(to_version):
                    applicable_migrations.append(key)

        for migration in applicable_migrations:
            required_migrations.append(migration)

    return required_migrations


async def migrate() -> None:
    required_migrations = await detect_required_migrations()
    if not required_migrations:
        rich.print("[yellow]No migrations required.")
        return

    rich.print("[green]Starting migration process...")

    backup_data()

    applied_migrations = set()

    while required_migrations:
        for migration_key in required_migrations:
            if migration_key in applied_migrations:
                continue

            component, from_version, to_version = migration_key
            migration_func = migration_registry[migration_key]

            rich.print(f"[green]Running migration: {component} {from_version} -> {to_version}")
            await migration_func()
            applied_migrations.add(migration_key)

        new_required_migrations = await detect_required_migrations()
        required_migrations = [m for m in new_required_migrations if m not in applied_migrations]

        if not required_migrations:
            rich.print("[green]No more migrations required.")

    rich.print(
        f"[green]All migrations completed successfully. Applied {len(applied_migrations)} migrations in total."
    )


def die(message: str) -> NoReturn:
    rich.print(f"[red]{message}")
    print(message, file=sys.stderr)
    sys.exit(1)


def main() -> None:
    try:
        asyncio.run(migrate())
    except Exception as e:
        die(str(e))


if __name__ == "__main__":
    main()
