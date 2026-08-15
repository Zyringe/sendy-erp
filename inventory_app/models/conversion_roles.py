"""Central validator for conversion_formula_inputs.role (mig 158).

Scope, exactly as specified in the design doc (Phase 0 "Central validation
helper" table, docs/plans/2026-08-14-hammer-pack-bundle-plan.md §5/§9):

    | Applies to    | active formulas whose `name` starts with '[แพ็ค]' — and nothing else |
    | Single input  | role may be NULL (legacy shape) or 'component' |
    | Multi input   | exactly one 'component' AND exactly one 'packaging' |
    | Everything else | NOT interpreted: [แกะ] halves, the inactive combo |
    |               | marker (fid 126), any future N-input recipe |
    | Fail closed   | a multi-input [แพ็ค] with no roles, 0 components, or |
    |               | >1 component is a CONFIGURATION ERROR — raise, never guess |

Deliberately NOT keyed on `len(inputs) > 1`: that would silently forbid any
future active multi-component manufacturing formula, a policy this module
has no business setting. Key on the '[แพ็ค]' prefix + is_active instead.

Row ORDER must never carry meaning — conversion_formula_inputs has no index
at all and the three existing readers (models/conversions.py:294,
models/bsn_sync.py:87, models/conversions.py:401) read it with no ORDER BY.
Every function here is proven order-independent by its tests.
"""

ROLE_COMPONENT = 'component'
ROLE_PACKAGING = 'packaging'

_PACK_PREFIX = '[แพ็ค]'


class ConversionRoleError(Exception):
    """A [แพ็ค] formula's role assignment does not match the contract above.
    Message is Thai and operator-readable — the app flashes it directly."""


def is_pack_formula(name, is_active):
    """True only for an ACTIVE formula whose name starts with '[แพ็ค]'.
    This is the exact scope gate — everything else in this module trusts it."""
    return bool(is_active) and (name or '').startswith(_PACK_PREFIX)


def _get(row, key):
    """Accept both sqlite3.Row and plain dict — sqlite3.Row has no .get()."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _classify(inputs):
    """Split inputs into (components, packaging, unroled) lists, order-
    independent by construction (a single pass, no reliance on row position)."""
    components, packaging, unroled = [], [], []
    for row in inputs:
        role = _get(row, 'role')
        if role == ROLE_COMPONENT:
            components.append(row)
        elif role == ROLE_PACKAGING:
            packaging.append(row)
        else:
            unroled.append(row)
    return components, packaging, unroled


def validate_pack_inputs(name, is_active, inputs):
    """Validate a formula's input rows against the role contract.

    Returns None for a formula outside this validator's scope (not an
    active [แพ็ค] formula) — explicitly not interpreted, not implicitly
    approved.

    Raises ConversionRoleError for an in-scope formula whose roles violate
    the contract. Returns None (implicitly) when the shape is valid.
    """
    if not is_pack_formula(name, is_active):
        return None

    inputs = list(inputs)
    if len(inputs) == 1:
        role = _get(inputs[0], 'role')
        if role not in (None, ROLE_COMPONENT):
            raise ConversionRoleError(
                f'สูตร "{name}": input เดียวต้องมี role เป็นว่าง หรือ component เท่านั้น (พบ {role!r})'
            )
        return None

    components, packaging, unroled = _classify(inputs)
    if len(components) != 1 or len(packaging) != 1 or unroled:
        raise ConversionRoleError(
            f'สูตร "{name}": ชุดแพ็คหลาย input ต้องมี role "component" 1 รายการ '
            f'และ "packaging" 1 รายการเท่านั้น '
            f'(พบ component={len(components)}, packaging={len(packaging)}, '
            f'ไม่มี role={len(unroled)}, รวม={len(inputs)} rows)'
        )
    return None


def component_product_id(name, is_active, inputs):
    """The partner product id for cross_unit_hazard: the sole input when
    single-input (role NULL or component); the role='component' row's
    product_id when multi-input and valid.

    Raises ConversionRoleError for anything out of scope (not an active
    [แพ็ค] formula) or invalid — this function has exactly one job and must
    never silently guess a partner.
    """
    if not is_pack_formula(name, is_active):
        raise ConversionRoleError(
            f'component_product_id เรียกกับสูตรที่ไม่ใช่ [แพ็ค] ที่ active: "{name}"'
        )

    inputs = list(inputs)
    validate_pack_inputs(name, is_active, inputs)  # raises on violation

    if len(inputs) == 1:
        return _get(inputs[0], 'product_id')

    components, _packaging, _unroled = _classify(inputs)
    return _get(components[0], 'product_id')
