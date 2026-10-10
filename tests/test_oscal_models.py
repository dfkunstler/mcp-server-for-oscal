"""Property-based tests for the OSCAL model classes used by the store and validator.

Every parse in ``OscalStore`` goes through ``MODEL_MAP`` in ``tools/utils.py``. These
properties check that the mapping and the parsed results behave as the design requires.
"""

import copy
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_for_oscal.tools.oscal_store import OscalStore
from mcp_server_for_oscal.tools.utils import MODEL_MAP, OSCALModelType
from mcp_server_for_oscal.tools.validate_oscal_content import _validate_model

from .tools.test_oscal_store import _VALID_DOC_BUILDERS, _make_mapping_collection

_uuids = st.uuids(version=4).map(str)

# Non-empty titles with no leading or trailing whitespace.
_titles = st.text(
    alphabet=st.characters(whitelist_categories=("L", "N")),
    min_size=1,
    max_size=40,
)

# Builders keyed by root key; mapping-collection adds a fourth model type.
_BUILDERS: dict = {
    **_VALID_DOC_BUILDERS,
    "mapping-collection": lambda uuid, _title: _make_mapping_collection([uuid]),
}

_root_keys = st.sampled_from(sorted(_BUILDERS))


def _add_unknown_root_key(inner: dict) -> None:
    inner["bogus-field"] = 1


def _add_unknown_metadata_key(inner: dict) -> None:
    inner["metadata"]["bogus-field"] = 1


def _delete_uuid(inner: dict) -> None:
    del inner["uuid"]


def _delete_metadata(inner: dict) -> None:
    del inner["metadata"]


# Each mutation edits the object under the root key, which is what both
# validators pass to the model class. None means "leave the document valid".
_MUTATIONS = {
    "unknown-root-key": _add_unknown_root_key,
    "unknown-metadata-key": _add_unknown_metadata_key,
    "delete-uuid": _delete_uuid,
    "delete-metadata": _delete_metadata,
}

_mutations = st.one_of(st.none(), st.sampled_from(sorted(_MUTATIONS)))

# Offsets allowed by the OSCAL DateTimeWithTimezoneDatatype pattern. The pattern does not
# accept every minute in -14:00..+14:00: it allows whole hours from -12:00 to +14:00 plus
# the real-world half- and three-quarter-hour zones listed below. Offsets outside this set
# would produce documents that are not valid OSCAL, so the strategy stays inside it.
_HALF_AND_QUARTER_OFFSETS = [
    (-1, 3, 30),
    (-1, 9, 30),
    *((1, h, 30) for h in (3, 4, 5, 6, 9, 10)),
    *((1, h, 45) for h in (5, 8, 12)),
]
_OSCAL_UTC_OFFSETS = sorted(
    {timedelta(hours=-h) for h in range(13)}
    | {timedelta(hours=h) for h in range(15)}
    | {sign * timedelta(hours=h, minutes=m) for sign, h, m in _HALF_AND_QUARTER_OFFSETS}
)

# Naive wall-clock times. The pattern only accepts years 19xx and 2xxx.
# st.datetimes requires naive bounds when generating naive values, hence the noqa.
_naive_datetimes = st.datetimes(
    min_value=datetime(1900, 1, 1),  # noqa: DTZ001 - naive bound for naive strategy
    max_value=datetime(2999, 12, 31),  # noqa: DTZ001 - naive bound for naive strategy
)


@pytest.mark.unit
class TestOscalModelProperties:
    """Properties of typed OSCAL parsing through ``MODEL_MAP``."""

    # Feature: oscal-bindings migration (#26), Property 1: Typed parse returns the mapped class
    @given(root_key=_root_keys, doc_uuid=_uuids, title=_titles)
    @settings(max_examples=100, deadline=None)
    def test_typed_parse_returns_mapped_class(self, root_key, doc_uuid, title):
        """A valid document parses to exactly ``MODEL_MAP[model_type]``.

        **Validates: Requirements 3.1, 2.4**
        """
        model_type = OSCALModelType(root_key)
        doc = _BUILDERS[root_key](doc_uuid, title)

        parsed = OscalStore._do_parse(json.dumps(doc), model_type.value)

        assert type(parsed) is MODEL_MAP[model_type]

    # Feature: oscal-bindings migration (#26), Property 2: Store and validator agree with the acceptance oracle
    @given(root_key=_root_keys, doc_uuid=_uuids, title=_titles, mutation=_mutations)
    @settings(max_examples=100, deadline=None)
    def test_store_and_validator_agree_with_oracle(self, root_key, doc_uuid, title, mutation):
        """Store and validator accept a document exactly when it was not mutated.

        **Validates: Requirements 7.1, 7.2, 7.3, 3.2, 2.5**
        """
        model_type = OSCALModelType(root_key)
        doc = copy.deepcopy(_BUILDERS[root_key](doc_uuid, title))
        if mutation is not None:
            _MUTATIONS[mutation](doc[root_key])
        expected = mutation is None

        with tempfile.TemporaryDirectory() as tmp:
            store = OscalStore(
                db_path=str(Path(tmp) / "test.db"), cache_size=1, seed_from_bundled=False
            )
            try:
                store_valid = store._validate_with_model(doc, model_type)
            finally:
                store.close()

        validator_valid = _validate_model(doc, model_type)["valid"]

        assert store_valid == validator_valid
        assert store_valid is expected

    # Feature: oscal-bindings migration (#26), Property 5: Datetime offsets are preserved
    @given(
        naive=_naive_datetimes,
        offset=st.sampled_from(_OSCAL_UTC_OFFSETS),
        doc_uuid=_uuids,
        title=_titles,
    )
    @settings(max_examples=100, deadline=None)
    def test_datetime_offsets_are_preserved(self, naive, offset, doc_uuid, title):
        """``metadata.last-modified`` keeps its UTC offset and wall-clock time after parsing.

        **Validates: Requirements 8.1**
        """
        doc = copy.deepcopy(_BUILDERS["component-definition"](doc_uuid, title))
        # isoformat() always emits seconds (required by the pattern) and adds
        # microseconds only when they are non-zero.
        stamp = naive.replace(tzinfo=timezone(offset)).isoformat()
        doc["component-definition"]["metadata"]["last-modified"] = stamp

        parsed = OscalStore._do_parse(json.dumps(doc), "component-definition")
        last_modified = parsed.metadata.last_modified

        assert last_modified.utcoffset() == offset
        assert last_modified.replace(tzinfo=None) == naive
