/** Parse JSON while rejecting duplicate object keys before values can be overwritten. */
export function parseUniqueJson(text: string): unknown {
  let offset = 0;

  function skipWhitespace(): void {
    while (offset < text.length && /[\t\n\r ]/.test(text[offset])) offset += 1;
  }

  function parseString(): string {
    if (text[offset] !== '"') throw new SyntaxError(`expected JSON string at ${offset}`);
    const start = offset;
    offset += 1;
    while (offset < text.length) {
      const character = text[offset];
      if (character === '"') {
        offset += 1;
        return JSON.parse(text.slice(start, offset)) as string;
      }
      if (character === "\\") {
        offset += 2;
      } else {
        if (character.charCodeAt(0) < 0x20) {
          throw new SyntaxError(`unescaped control character at ${offset}`);
        }
        offset += 1;
      }
    }
    throw new SyntaxError("unterminated JSON string");
  }

  function parseObject(): Record<string, unknown> {
    offset += 1;
    skipWhitespace();
    const value: Record<string, unknown> = Object.create(null) as Record<string, unknown>;
    const keys = new Set<string>();
    if (text[offset] === "}") {
      offset += 1;
      return value;
    }
    while (offset < text.length) {
      const key = parseString();
      if (keys.has(key)) throw new SyntaxError(`duplicate JSON object key ${JSON.stringify(key)}`);
      keys.add(key);
      skipWhitespace();
      if (text[offset] !== ":") throw new SyntaxError(`expected ':' at ${offset}`);
      offset += 1;
      value[key] = parseValue();
      skipWhitespace();
      if (text[offset] === "}") {
        offset += 1;
        return value;
      }
      if (text[offset] !== ",") throw new SyntaxError(`expected ',' at ${offset}`);
      offset += 1;
      skipWhitespace();
    }
    throw new SyntaxError("unterminated JSON object");
  }

  function parseArray(): unknown[] {
    offset += 1;
    skipWhitespace();
    const value: unknown[] = [];
    if (text[offset] === "]") {
      offset += 1;
      return value;
    }
    while (offset < text.length) {
      value.push(parseValue());
      skipWhitespace();
      if (text[offset] === "]") {
        offset += 1;
        return value;
      }
      if (text[offset] !== ",") throw new SyntaxError(`expected ',' at ${offset}`);
      offset += 1;
      skipWhitespace();
    }
    throw new SyntaxError("unterminated JSON array");
  }

  function parseValue(): unknown {
    skipWhitespace();
    const character = text[offset];
    if (character === '"') return parseString();
    if (character === "{") return parseObject();
    if (character === "[") return parseArray();
    for (const [literal, value] of [
      ["true", true],
      ["false", false],
      ["null", null],
    ] as const) {
      if (text.startsWith(literal, offset)) {
        offset += literal.length;
        return value;
      }
    }
    const match = /^-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?/.exec(text.slice(offset));
    if (!match) throw new SyntaxError(`invalid JSON value at ${offset}`);
    offset += match[0].length;
    const number = Number(match[0]);
    if (!Number.isFinite(number)) throw new SyntaxError(`non-finite JSON number at ${offset}`);
    return number;
  }

  const value = parseValue();
  skipWhitespace();
  if (offset !== text.length) throw new SyntaxError(`unexpected JSON content at ${offset}`);
  return value;
}
