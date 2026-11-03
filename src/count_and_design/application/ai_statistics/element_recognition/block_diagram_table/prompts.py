"""LLM prompts and token limits for block-diagram table extraction."""

from __future__ import annotations

BLOCK_DIAGRAM_TABLE_INSTRUCTIONS = """Extract the device ID and PV string count from the table below. Only JSON format data is returned.

Use the device inventory table: This table contains device/power source identifier columns (e.g., FROM, combiner box, load circuit breaker, etc.) and string count columns for each row (e.g., STRINGS, NUMBER OF STRINGS, STRING COUNT, etc.).

Columns:

- Device ID: A unique identifier for each CBX/LBD. Fill in the table exactly as it says. Also, try to ensure the device ID format is consistent across all outputs in the same diagram to prevent errors due to individual characters.

- String Count: STRING/COMBINER, STRING/LOAD BREAK DISCONNECT, NO. OF STRINGS, STRINGS. For the same device, there may be two rows, labeled MESSENGER/DIRECT BURY/OPEN AIR respectively. This distinguishes the installation type, but the string count in both rows is the same.

Determine equipment_family based on table content: Return "CBX" when the device is a combiner box, and "LBD" when the device is a load circuit breaker.

Return exactly ONE JSON object. Use normal single braces only — never doubled braces.
Do NOT wrap each row in its own object and do NOT return a JSON array: all rows belong inside the single string_info list.

Example:
```json
{"equipment_family":"CBX","string_info":[{"node":"INV1101-CBX-12","string_number":45}]}
```
"""

BLOCK_DIAGRAM_TABLE_VISION_PROMPT = (
    BLOCK_DIAGRAM_TABLE_INSTRUCTIONS
    + "\n\nRead the cropped table image and return the same JSON."
)
