"""Conservative, offline Spring mapping discovery using a Java lexical scanner.

This is a declaration parser, not a Java compiler. It deliberately does not
evaluate constants, execute Java, or resolve inherited/meta-annotation mappings.
Only literal standard Spring annotation arguments establish resolved identities.
"""

from dataclasses import dataclass, field
import re

from .identity import CONDITION_KEYS, identify, safe_condition
from .inputs import iter_files
from .models import ScanResult
from .path_normalizer import normalize_path


_MAPPINGS = {"RequestMapping": None, "GetMapping": "GET", "PostMapping": "POST",
             "PutMapping": "PUT", "DeleteMapping": "DELETE", "PatchMapping": "PATCH"}
_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS", "TRACE"}
_MUTATION_DECLARATION = re.compile(
    r"^(?:add|insert|update|upd|delete|del|remove|create|save|reset|set|upload|import|"
    r"enable|disable|activate|deactivate|grant|revoke|bind|unbind)(?:[A-Z_]|$)"
)
_MODIFIERS = {"public", "protected", "private", "static", "final", "abstract",
              "synchronized", "native", "strictfp", "default"}
_STANDARD_ANNOTATIONS = {name: "org.springframework.web.bind.annotation." + name
                         for name in {*_MAPPINGS, "RestController", "ResponseBody"}}
_STANDARD_ANNOTATIONS.update({"Controller": "org.springframework.stereotype.Controller",
                              "Deprecated": "java.lang.Deprecated"})
_REQUEST_METHOD = "org.springframework.web.bind.annotation.RequestMethod"
_SPRING_ANNOTATION_PACKAGES = {"org.springframework.web.bind.annotation", "org.springframework.stereotype"}
_KNOWN_RETURN_TYPES = {
    "ResponseEntity": "org.springframework.http.ResponseEntity",
    "HttpEntity": "org.springframework.http.HttpEntity",
    "ModelAndView": "org.springframework.web.servlet.ModelAndView",
    "View": "org.springframework.web.servlet.View",
    "RedirectView": "org.springframework.web.servlet.view.RedirectView",
    "String": "java.lang.String",
}
_RETURN_TYPE_WILDCARD_PACKAGES = _SPRING_ANNOTATION_PACKAGES | {
    name.rsplit(".", 1)[0] for name in _KNOWN_RETURN_TYPES.values()
}
_LIMITATIONS = [
    "Java constants, concatenation and other expressions are not evaluated; affected mappings remain unresolved.",
    "Inherited controller/interface mappings and meta-annotation semantics are not resolved. Locally declared composed mappings remain unresolved; external custom annotations without available mapping metadata may not be recognizable.",
    "Spring annotation and RequestMethod semantics require an exact FQN or proven explicit/wildcard import; same-package/local declarations and competing wildcard imports remain unresolved. Full Java symbol resolution and static imports are unsupported.",
    "Known HTTP/view return types require proven FQN/import provenance; visible same-package/local declarations or type parameters block simple-name inference. String uses java.lang implicit import absent visible shadows. Annotation wildcard policy is unchanged.",
    "HTTP exposure requires an explicit Controller or RestController annotation; bean registration, web.xml and WebServlet exposure are not inferred.",
    "Page classification requires a clear view type or exclusively literal String returns; other Controller returns remain unknown.",
    "Java text-block mapping literals, Spring SpEL paths, Ant question-mark patterns, and Unicode escapes that may change lexical boundaries remain unresolved or are reported conservatively.",
    "Mutation candidates use explicit Java operation declaration prefixes; method names require semantic review and may not identify every write operation.",
]


@dataclass(frozen=True)
class _Token:
    kind: str
    text: str
    line: int


@dataclass
class _Annotation:
    name: str
    line: int
    args: list[_Token] = field(default_factory=list)
    qualified_name: str = ""


@dataclass
class _Mapping:
    annotation: _Annotation
    paths: list[str] = field(default_factory=lambda: [""])
    methods: list[str] = field(default_factory=list)
    conditions: dict = field(default_factory=lambda: {key: [] for key in CONDITION_KEYS})
    unresolved: set[str] = field(default_factory=set)


def _java_string(text):
    """Decode an ordinary Java string without evaluating Java expressions."""
    result = []
    i = 1
    end = len(text) - 1
    escapes = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r",
               '"': '"', "'": "'", "\\": "\\", "s": " "}
    while i < end:
        char = text[i]
        if char != "\\":
            result.append(char)
            i += 1
            continue
        i += 1
        if i >= end:
            raise ValueError("Invalid string escape")
        char = text[i]
        if char in escapes:
            result.append(escapes[char])
            i += 1
        elif char == "u":
            while i < end and text[i] == "u":
                i += 1
            digits = text[i:i + 4]
            if len(digits) != 4 or not all(c in "0123456789abcdefABCDEF" for c in digits):
                raise ValueError("Invalid Unicode string escape")
            decoded = chr(int(digits, 16))
            # Java preprocesses Unicode escapes before tokenization. Escapes that
            # change token boundaries cannot safely be treated as string data.
            if decoded in {'"', "\\", "\n", "\r"}:
                raise ValueError("Unicode escape changes lexical boundaries")
            result.append(decoded)
            i += 4
        elif char in "01234567":
            maximum = 3 if char in "0123" else 2
            digits = char
            i += 1
            while len(digits) < maximum and i < end and text[i] in "01234567":
                digits += text[i]
                i += 1
            result.append(chr(int(digits, 8)))
        else:
            raise ValueError("Unsupported string escape")
    return "".join(result)


def _lex(source):
    tokens = []
    i, line = 0, 1
    while i < len(source):
        char = source[i]
        if char.isspace():
            line += char == "\n"
            i += 1
        elif source.startswith("//", i):
            end = source.find("\n", i + 2)
            if _unsafe_comment_escape(source[i:len(source) if end < 0 else end]):
                raise ValueError("Unicode escape inside Java comment")
            i = len(source) if end < 0 else end
        elif source.startswith("/*", i):
            end = source.find("*/", i + 2)
            if end < 0:
                raise ValueError("Unterminated Java comment")
            if _unsafe_comment_escape(source[i:end + 2]):
                raise ValueError("Unicode escape inside Java comment")
            line += source[i:end + 2].count("\n")
            i = end + 2
        elif source.startswith('"""', i):
            end = i + 3
            while True:
                end = source.find('"""', end)
                if end < 0:
                    raise ValueError("Unterminated Java text block")
                preceding = 0
                p = end - 1
                while p >= i and source[p] == "\\":
                    preceding += 1
                    p -= 1
                if preceding % 2 == 0:
                    break
                end += 3
            tokens.append(_Token("text_block", "", line))
            line += source[i:end + 3].count("\n")
            i = end + 3
        elif char in {'"', "'"}:
            start, start_line, quote = i, line, char
            i += 1
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == quote:
                    i += 1
                    break
                elif source[i] in "\n\r":
                    raise ValueError("Unterminated Java literal")
                else:
                    i += 1
            else:
                raise ValueError("Unterminated Java literal")
            literal = source[start:i]
            # Unicode preprocessing can expose syntax even in a non-mapping
            # literal. Reject boundary-changing escapes throughout the file.
            if "\\u" in literal:
                _java_string(literal)
                if quote == "'" and re.search(r"\\u+0027", literal, re.IGNORECASE):
                    raise ValueError("Unicode escape changes lexical boundaries")
            tokens.append(_Token("string" if quote == '"' else "char", literal, start_line))
        elif char == "\\" and i + 1 < len(source) and source[i + 1] == "u":
            raise ValueError("Unicode escape outside string")
        elif char.isalpha() or char in "_$":
            end = i + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                end += 1
            tokens.append(_Token("identifier", source[i:end], line))
            i = end
        else:
            tokens.append(_Token("symbol", char, line))
            i += 1
    return tokens


def _unsafe_comment_escape(comment):
    # Quotes and letters remain comment text after Java Unicode preprocessing;
    # line terminators or comment delimiters can expose previously hidden code.
    for escape in re.finditer(r"\\u+", comment):
        digits = comment[escape.end():escape.end() + 4]
        if len(digits) != 4 or not all(char in "0123456789abcdefABCDEF" for char in digits):
            return True
        if chr(int(digits, 16)) in "\n\r/*\\":
            return True
    return False


def _balanced(tokens):
    pairs, stack = {}, []
    for i, token in enumerate(tokens):
        if token.kind != "symbol":
            continue
        if token.text in {"(", "[", "{"}:
            stack.append((token.text, i))
        elif token.text in {")", "]", "}"}:
            if not stack or stack[-1][0] != {")": "(", "]": "[", "}": "{"}[token.text]:
                raise ValueError("Unbalanced Java delimiters")
            _, start = stack.pop()
            pairs[start] = i
    if stack:
        raise ValueError("Unbalanced Java delimiters")
    return pairs


def _split(tokens, separator=","):
    parts, start, depth = [], 0, 0
    for i, token in enumerate(tokens):
        if token.kind == "symbol":
            if token.text in {"(", "[", "{"}:
                depth += 1
            elif token.text in {")", "]", "}"}:
                depth -= 1
            elif token.text == separator and depth == 0:
                parts.append(tokens[start:i])
                start = i + 1
    parts.append(tokens[start:])
    return parts


def _literal_values(tokens, methods=False, resolve_type=None):
    if not tokens:
        return [], False
    if tokens[0].text == "{" and tokens[-1].text == "}":
        values = _split(tokens[1:-1]) if len(tokens) > 2 else []
        if values and not values[-1]:  # Java allows a trailing array comma.
            values.pop()
    else:
        values = [tokens]
    result = []
    valid = True
    for value in values:
        if not methods and len(value) == 1 and value[0].kind == "string":
            try:
                result.append(_java_string(value[0].text))
            except ValueError:
                valid = False
        elif methods:
            text = "".join(token.text for token in value)
            match = re.fullmatch(r"((?:[\w$]+\.)*RequestMethod)\.([A-Z]+)", text)
            if (match and match.group(2) in _METHODS and resolve_type is not None
                    and resolve_type(match.group(1), _REQUEST_METHOD) == _REQUEST_METHOD):
                result.append(match.group(2))
            else:
                valid = False
        else:
            valid = False
    return sorted(set(result)), valid


def _annotation_names(annotations):
    return {annotation.name for annotation in annotations
            if _STANDARD_ANNOTATIONS.get(annotation.name) == annotation.qualified_name}


def _strip_return_prefix(tokens):
    tokens = list(tokens)
    while tokens and tokens[0].text in _MODIFIERS:
        tokens.pop(0)
    if tokens and tokens[0].text == "<":
        depth, end = 0, 0
        for end, token in enumerate(tokens):
            depth += token.text == "<"
            depth -= token.text == ">"
            if depth == 0:
                break
        tokens = tokens[end + 1:]
    return tokens


def _mapping(annotation, custom=False, resolve_type=None):
    result = _Mapping(annotation)
    if custom:
        result.paths = []
        result.unresolved.add("custom_composed_mapping")
        return result
    attributes = {}
    for argument in _split(annotation.args) if annotation.args else []:
        assignments = _split(argument, "=")
        if len(assignments) == 2 and len(assignments[0]) == 1 and assignments[0][0].kind == "identifier":
            key, expression = assignments[0][0].text, assignments[1]
        elif len(assignments) == 1:
            key, expression = "value", argument
        else:
            result.unresolved.add("annotation_arguments")
            continue
        if key in attributes:
            result.unresolved.add(key)
        attributes[key] = expression
    for key in attributes:
        if key not in {"value", "path", "method", *CONDITION_KEYS, "name"}:
            result.unresolved.add("unsupported_attribute")
    path_values = []
    for key in ("value", "path"):
        if key in attributes:
            values, valid = _literal_values(attributes[key])
            path_values.append(values)
            if not valid:
                result.unresolved.add(key)
    if path_values:
        result.paths = path_values[0] or [""]
        if len(path_values) == 2 and path_values[0] != path_values[1]:
            result.unresolved.add("path_alias_conflict")
    if "value" in result.unresolved or "path" in result.unresolved:
        result.paths = []
    if any("?" in path or "#" in path for path in result.paths):
        result.paths = []
        result.unresolved.add("unsupported_source_path_pattern")
    fixed_method = _MAPPINGS[annotation.name]
    if fixed_method:
        result.methods = [fixed_method]
    if "method" in attributes:
        methods, valid = _literal_values(attributes["method"], methods=True, resolve_type=resolve_type)
        if not valid or fixed_method:
            result.unresolved.add("method")
        result.methods = methods if valid and not fixed_method else []
    for key in CONDITION_KEYS:
        if key in attributes:
            values, valid = _literal_values(attributes[key])
            if not valid:
                result.unresolved.add(key)
            # Keep literals private until identity construction projects the
            # shared safe condition representation into the public record.
            result.conditions[key] = sorted({value.strip() for value in values if value.strip()})
    return result


def _package_name(tokens):
    for i, token in enumerate(tokens):
        if token.text == "package":
            end = i + 1
            while end < len(tokens) and tokens[end].text != ";":
                end += 1
            return "".join(t.text for t in tokens[i + 1:end])
        if token.text in {"class", "interface", "enum", "record"}:
            break
    return ""


def _declared_types(tokens):
    """Visible source names are shadow guards, not a Java symbol solver."""
    return {tokens[i + 1].text for i, token in enumerate(tokens[:-1])
            if token.kind == "identifier" and token.text in {"class", "interface", "enum", "record"}
            and tokens[i + 1].kind == "identifier"}


def _type_parameter_names(tokens):
    """Read declaration type-parameter names without interpreting their bounds."""
    if not tokens or tokens[0].text != "<":
        return set()
    names, depth, needs_name, i = set(), 0, True, 0
    while i < len(tokens):
        token = tokens[i]
        if depth == 1 and needs_name and token.text == "@":
            # Type-use annotations precede the declared name, not its bound.
            i += 1
            if i < len(tokens) and tokens[i].kind == "identifier":
                i += 1
                while (i + 1 < len(tokens) and tokens[i].text == "."
                       and tokens[i + 1].kind == "identifier"):
                    i += 2
            if i < len(tokens) and tokens[i].text == "(":
                parentheses = 1
                i += 1
                while i < len(tokens) and parentheses:
                    parentheses += tokens[i].text == "("
                    parentheses -= tokens[i].text == ")"
                    i += 1
            continue
        if token.text == "<":
            depth += 1
        elif token.text == ">":
            depth -= 1
            if depth == 0:
                break
        elif depth == 1 and token.text == ",":
            needs_name = True
        elif depth == 1 and needs_name and token.kind == "identifier":
            names.add(token.text)
            needs_name = False
        i += 1
    return names


class _Parser:
    def __init__(self, tokens, file, service, custom, result, package_types=None):
        self.tokens = tokens
        self.pairs = _balanced(tokens)
        self.file, self.service, self.custom, self.result = file, service, custom, result
        self.package = _package_name(tokens)
        self.imports, self.wildcard_imports = self._imports()
        self.shadowed_types = _declared_types(tokens) | (package_types or {}).get(self.package, set())
        # A file-level conservative guard also covers nested class type variables;
        # method type variables are checked in their own return declaration.
        self.return_type_parameters = set()
        for i, token in enumerate(tokens[:-2]):
            if (token.text in {"class", "interface", "record"} and tokens[i + 1].kind == "identifier"
                    and tokens[i + 2].text == "<"):
                self.return_type_parameters.update(_type_parameter_names(tokens[i + 2:]))
        self.foreign_wildcard_same_name = False

    def _imports(self):
        imports, wildcards = {}, set()
        for i, token in enumerate(self.tokens):
            if token.text in {"class", "interface", "enum", "record"}:
                break
            if token.text == "import":
                end = i + 1
                while end < len(self.tokens) and self.tokens[end].text != ";":
                    end += 1
                name = "".join(t.text for t in self.tokens[i + 1:end])
                if name.startswith("static"):
                    continue
                if name.endswith(".*"):
                    wildcards.add(name[:-2])
                else:
                    simple = name.rsplit(".", 1)[-1]
                    imports[simple] = name if simple not in imports or imports[simple] == name else ""
        return imports, wildcards

    def _resolve_type(self, name, expected, wildcard_packages=None):
        if "." in name:
            return name  # FQNs never inherit a simple-name import or shadow.
        if name in self.shadowed_types:
            return name
        if name in self.imports:
            return self.imports[name]
        if expected in {"java.lang.Deprecated", "java.lang.String"}:
            return expected  # Java's implicit java.lang import, absent a visible shadow.
        package = expected.rsplit(".", 1)[0]
        if (package in self.wildcard_imports
                and self.wildcard_imports <= (wildcard_packages if wildcard_packages is not None
                                              else _SPRING_ANNOTATION_PACKAGES)):
            return expected
        return name

    def _annotation(self, start):
        i = start + 1
        if i >= len(self.tokens) or self.tokens[i].kind != "identifier":
            raise ValueError("Invalid Java annotation")
        names = [self.tokens[i].text]
        i += 1
        while i + 1 < len(self.tokens) and self.tokens[i].text == "." and self.tokens[i + 1].kind == "identifier":
            names.append(self.tokens[i + 1].text)
            i += 2
        args = []
        if i < len(self.tokens) and self.tokens[i].text == "(":
            end = self.pairs[i]
            args = self.tokens[i + 1:end]
            i = end + 1
        raw_name = ".".join(names)
        expected = _STANDARD_ANNOTATIONS.get(names[-1])
        qualified_name = (self._resolve_type(raw_name, expected) if expected
                          else raw_name if len(names) > 1 else self.imports.get(names[0], names[0]))
        if (len(names) == 1 and expected and any(not package.startswith("org.springframework.")
                                              for package in self.wildcard_imports)):
            self.foreign_wildcard_same_name = True
        return _Annotation(names[-1], self.tokens[start].line, args, qualified_name), i

    def _declarations(self, start, end):
        i = start
        while i < end:
            annotations, header = [], []
            while i < end:
                token = self.tokens[i]
                if token.text == "@" and i + 1 < end and self.tokens[i + 1].text != "interface":
                    annotation, i = self._annotation(i)
                    annotations.append(annotation)
                elif token.text in {"(", "["}:
                    close = self.pairs[i]
                    header.extend(self.tokens[i:close + 1])
                    i = close + 1
                elif token.text in {";", "{"}:
                    terminator = token.text
                    body_start = i + 1
                    body_end = self.pairs[i] if terminator == "{" else i
                    yield annotations, header, body_start, body_end, terminator
                    i = body_end + 1
                    break
                else:
                    header.append(token)
                    i += 1
            else:
                if annotations or header:
                    raise ValueError("Incomplete Java declaration")

    @staticmethod
    def _class(header):
        for i, token in enumerate(header[:-1]):
            if token.kind == "identifier" and token.text in {"class", "interface", "enum", "record"}:
                if header[i + 1].kind == "identifier":
                    annotation_type = i > 0 and header[i - 1].text == "@"
                    return header[i + 1].text, annotation_type
        return None

    @staticmethod
    def _method(header, class_name):
        depth, candidate = 0, None
        for i, token in enumerate(header):
            if token.text == "=" and depth == 0:
                if candidate is not None:
                    raise ValueError("Assignment after method declaration")
                return None
            if token.text == "(":
                if depth == 0 and candidate is None and i and header[i - 1].kind == "identifier":
                    name = header[i - 1].text
                    return_tokens = header[:i - 1]
                    if not any(token.kind == "identifier" for token in _strip_return_prefix(return_tokens)):
                        if name == class_name:
                            return None  # Constructor, including generic constructors.
                        raise ValueError("Method declaration lacks return type")
                    candidate = name, return_tokens
                depth += 1
            elif token.text == ")":
                depth -= 1
        return candidate

    @staticmethod
    def _inherits(header):
        depth = 0
        for token in header:
            if token.text == "<":
                depth += 1
            elif token.text == ">":
                depth -= 1
            elif depth == 0 and token.text in {"extends", "implements"}:
                return True
        return False

    def _member_body_start(self, header, start, end):
        if any(token.text == "enum" for token in header):
            # Enum constants may have arguments and class bodies, and a final
            # semicolon is optional when the enum has no ordinary members.
            position = start
            while position < end:
                token = self.tokens[position]
                if token.text == ";":
                    return position + 1
                if token.text in {"(", "[", "{"}:
                    position = self.pairs[position] + 1
                else:
                    position += 1
            return end
        return start

    def _issue(self, category, annotation, **details):
        self.result.issues.append({"origin": "source", "category": category, "file": self.file,
                                   "line": annotation.line, "mapping_annotation": annotation.name, **details})

    def _mappings(self, annotations):
        mappings = []
        for annotation in annotations:
            spring_mapping = (annotation.name in _MAPPINGS
                              and annotation.qualified_name == _STANDARD_ANNOTATIONS[annotation.name])
            custom = (not spring_mapping and (annotation.name in self.custom
                      or (annotation.name.endswith("Mapping") and annotation.name not in _MAPPINGS)
                      or annotation.name in _MAPPINGS))
            if spring_mapping or custom:
                mapping = _mapping(annotation, custom, self._resolve_type)
                if annotation.name in _MAPPINGS and not spring_mapping:
                    mapping.unresolved.add("annotation_provenance")
                    self._issue("UNPROVEN_ANNOTATION_PROVENANCE", annotation,
                                reason="Spring mapping origin was not established from imports/FQN and visible declarations")
                mappings.append(mapping)
                if mapping.unresolved:
                    category = "CUSTOM_COMPOSED_MAPPING" if custom else "UNRESOLVED_JAVA_EXPRESSION"
                    self._issue(category, annotation, unresolved_attributes=sorted(mapping.unresolved))
        if len(mappings) > 1:
            for mapping in mappings:
                mapping.unresolved.add("multiple_mapping_annotations")
            self._issue("MULTIPLE_MAPPING_ANNOTATIONS", mappings[0].annotation)
        return mappings

    def _return_type(self, return_tokens):
        declaration = list(return_tokens)
        while declaration and declaration[0].text in _MODIFIERS:
            declaration.pop(0)
        type_parameters = self.return_type_parameters | _type_parameter_names(declaration)
        tokens = _strip_return_prefix(return_tokens)
        depth = 0
        for token in tokens:
            depth += token.text == "<"
            depth -= token.text == ">"
            if token.text == "[" and depth == 0:
                return ""
        base = []
        for token in tokens:
            if token.text == "<":
                break
            base.append(token.text)
        name = "".join(base)
        expected = _KNOWN_RETURN_TYPES.get(name.rsplit(".", 1)[-1])
        if expected:
            if "." not in name and name in type_parameters:
                return name
            return self._resolve_type(name, expected, _RETURN_TYPE_WILDCARD_PACKAGES)
        return self.imports.get(name, name)

    def _kind(self, class_annotations, method_annotations, return_tokens, body):
        names = _annotation_names(class_annotations + method_annotations)
        if "RestController" in names or ("Controller" in names and "ResponseBody" in names):
            return "HTTP_API"
        if "Controller" not in names:
            return "UNKNOWN_ROUTE"
        return_type = self._return_type(return_tokens)
        if return_type in {"org.springframework.http.ResponseEntity", "org.springframework.http.HttpEntity"}:
            return "HTTP_API"
        if return_type in {"org.springframework.web.servlet.ModelAndView",
                           "org.springframework.web.servlet.View", "org.springframework.web.servlet.view.RedirectView"}:
            return "PAGE_ROUTE"
        if return_type == "java.lang.String":
            returns = []
            for i, token in enumerate(body):
                if token.kind == "identifier" and token.text == "return":
                    end = i + 1
                    while end < len(body) and body[end].text != ";":
                        end += 1
                    returns.append(body[i + 1:end])
            if returns and all(len(value) == 1 and value[0].kind == "string" for value in returns):
                return "PAGE_ROUTE"
        return "UNKNOWN_ROUTE"

    def _record(self, class_info, method_info, mapping, class_mapping, annotations, body):
        controller, class_name, class_annotations = class_info
        java_method, return_tokens = method_info
        class_mapping = class_mapping or _Mapping(_Annotation("", 0))
        unresolved = mapping.unresolved | class_mapping.unresolved
        methods = sorted(set(class_mapping.methods + mapping.methods)) or ["ANY"]
        conditions = {}
        for key in CONDITION_KEYS:
            if key in {"params", "headers"}:
                conditions[key] = sorted(set(class_mapping.conditions[key] + mapping.conditions[key]))
            else:
                conditions[key] = mapping.conditions[key] or class_mapping.conditions[key]
        names = _annotation_names(class_annotations)
        active = bool(names & {"Controller", "RestController"})
        internal = any(annotation.name == "FeignClient" and annotation.qualified_name in {
            "FeignClient", "org.springframework.cloud.openfeign.FeignClient",
            "org.springframework.cloud.netflix.feign.FeignClient"} for annotation in class_annotations)
        kind = self._kind(class_annotations, annotations, return_tokens, body)
        deprecated = "Deprecated" in _annotation_names(class_annotations + annotations)
        class_paths = class_mapping.paths or [None]
        method_paths = mapping.paths or [None]
        for base in class_paths:
            for path in method_paths:
                runtime_path = None if base is None or path is None else "/" + "/".join(part.strip("/") for part in (base, path) if part)
                for method in methods:
                    status = "UNRESOLVED_JAVA_EXPRESSION" if unresolved else "AMBIGUOUS_METHOD" if method == "ANY" else "RESOLVED"
                    record = {"origin": "source", "file": self.file, "line": mapping.annotation.line,
                              "controller": controller, "java_method": java_method,
                              "mapping_annotation": mapping.annotation.name, "method": method,
                              "runtime_path": runtime_path,
                              "mapping_conditions": ({key: [safe_condition(value) for value in conditions[key]]
                                                      for key in CONDITION_KEYS} if unresolved else conditions),
                              "resolution_status": status, "api_kind": kind, "active": active,
                              "deprecated": deprecated, "internal": internal,
                              "mutation_candidate": bool(_MUTATION_DECLARATION.match(java_method)),
                              "identity": None, "api_uid": None, "display_identity": None}
                    if record["mutation_candidate"]:
                        record["mutation_evidence"] = "JAVA_OPERATION_DECLARATION"
                    if unresolved:
                        record["unresolved_attributes"] = sorted(unresolved)
                        if runtime_path is not None:
                            record.update(normalize_path(runtime_path, self.service))
                    elif runtime_path is not None:
                        identify(record, self.service)
                    if method == "ANY" and not unresolved:
                        self._issue("AMBIGUOUS_METHOD", mapping.annotation, controller=controller, java_method=java_method)
                    self.result.records.append(record)

    def _scan_class(self, annotations, header, body_start, body_end, enclosing):
        class_name, annotation_type = self._class(header)
        controller = ".".join(part for part in (self.package, enclosing, class_name) if part)
        if annotation_type:
            # Mapping meta-annotations describe annotations, not routes.
            return
        mappings = self._mappings(annotations)
        if self._inherits(header):
            inherited = _Mapping(_Annotation("ControllerInheritance", header[0].line), paths=[])
            inherited.unresolved.add("inheritance")
            if mappings:
                for mapping in mappings:
                    mapping.paths = []
                    mapping.unresolved.add("inheritance")
            else:
                mappings = [inherited]
            if _annotation_names(annotations) & {"Controller", "RestController"}:
                self._issue("INHERITED_MAPPING_UNSUPPORTED", inherited.annotation, controller=controller)
        member_scope = self._member_body_start(header, body_start, body_end)
        for member_annotations, member_header, member_start, member_end, member_terminator in self._declarations(member_scope, body_end):
            if self._class(member_header) and member_terminator == "{":
                # Nested controllers own their mapping prefix and stereotype.
                self._scan_class(member_annotations, member_header, member_start, member_end,
                                 ".".join(part for part in (enclosing, class_name) if part))
                continue
            method_info = self._method(member_header, class_name)
            if method_info is None:
                continue
            method_mappings = self._mappings(member_annotations)
            for mapping in method_mappings:
                for class_mapping in mappings or [None]:
                    self._record((controller, class_name, annotations), method_info, mapping, class_mapping,
                                 member_annotations, self.tokens[member_start:member_end] if member_terminator == "{" else [])

    def scan(self, start=0, end=None, enclosing=""):
        end = len(self.tokens) if end is None else end
        for annotations, header, body_start, body_end, terminator in self._declarations(start, end):
            class_info = self._class(header)
            if class_info and terminator == "{":
                self._scan_class(annotations, header, body_start, body_end, enclosing)


def _custom_mapping_annotations(files):
    """Find local mapping meta-annotations, without resolving their semantics."""
    custom = set()
    declarations = []
    def declarations_in_scope(parser, start, end):
        found = []
        for annotations, header, body_start, body_end, terminator in parser._declarations(start, end):
            class_info = parser._class(header)
            if class_info:
                if class_info[1]:
                    found.append((class_info[0], {annotation.name for annotation in annotations}))
                if terminator == "{":
                    member_start = parser._member_body_start(header, body_start, body_end)
                    found.extend(declarations_in_scope(parser, member_start, body_end))
        return found

    for file, tokens in files:
        parser = _Parser(tokens, file, "wizbank", set(), ScanResult())
        try:
            declarations.extend(declarations_in_scope(parser, 0, len(tokens)))
        except (ValueError, KeyError, IndexError):
            # The main pass reports this file and removes any partial records.
            continue
    changed = True
    while changed:
        changed = False
        for name, annotations in declarations:
            if name not in custom and annotations & (set(_MAPPINGS) | custom):
                custom.add(name)
                changed = True
    return custom


def scan_source(asset, service="wizbank") -> ScanResult:
    """Read a local source directory or ZIP and return safe mapping candidates."""
    result = ScanResult(limitations=list(_LIMITATIONS))
    files = []
    source_count = 0
    for file, content in iter_files(asset, {".java"}):
        source_count += 1
        try:
            source = content.decode("utf-8-sig")
            tokens = _lex(source)
            _balanced(tokens)
            files.append((file, tokens))
        except (UnicodeError, ValueError) as error:
            # Error wording is fixed by this scanner and never includes input.
            result.issues.append({"origin": "source", "category": "SOURCE_PARSE_ERROR", "file": file,
                                  "reason": "Unsupported source encoding" if isinstance(error, UnicodeError) else str(error)})
    package_types = {}
    for _, tokens in files:
        package_types.setdefault(_package_name(tokens), set()).update(_declared_types(tokens))
    custom = _custom_mapping_annotations(files)
    import_counts = {"source_spring_explicit_import_files": 0, "source_spring_wildcard_import_files": 0,
                     "source_spring_annotation_explicit_import_files": 0,
                     "source_spring_annotation_wildcard_import_files": 0,
                     "source_foreign_wildcard_same_name_annotation_files": 0}
    for file, tokens in files:
        first_record = len(result.records)
        parser = _Parser(tokens, file, service, custom, result, package_types)
        import_counts["source_spring_explicit_import_files"] += any(
            name.startswith("org.springframework.") for name in parser.imports.values())
        import_counts["source_spring_wildcard_import_files"] += any(
            name.startswith("org.springframework.") for name in parser.wildcard_imports)
        import_counts["source_spring_annotation_explicit_import_files"] += any(
            name in {*_STANDARD_ANNOTATIONS.values(), _REQUEST_METHOD} and name.startswith("org.springframework.")
            for name in parser.imports.values())
        import_counts["source_spring_annotation_wildcard_import_files"] += bool(
            parser.wildcard_imports & _SPRING_ANNOTATION_PACKAGES)
        try:
            parser.scan()
        except (ValueError, KeyError, IndexError):
            del result.records[first_record:]
            result.issues.append({"origin": "source", "category": "SOURCE_PARSE_ERROR", "file": file,
                                  "reason": "Unsupported or incomplete Java declaration"})
        import_counts["source_foreign_wildcard_same_name_annotation_files"] += parser.foreign_wildcard_same_name
    result.records.sort(key=lambda record: (record["file"], record["line"], record["controller"],
                                            record["java_method"], record["runtime_path"] or "", record["method"]))
    # A methodless Cartesian mapping emits one category per declaration, not per path.
    unique_issues = {repr(sorted(issue.items())): issue for issue in result.issues}
    result.issues = sorted(unique_issues.values(), key=lambda issue: (issue["file"], issue.get("line", 0), issue["category"]))
    result.counts = {"source_files": source_count, "source_candidates": len(result.records),
                     "source_parse_errors": sum(issue["category"] == "SOURCE_PARSE_ERROR" for issue in result.issues),
                     **import_counts,
                     "source_unproven_mapping_annotations": sum(
                         issue["category"] == "UNPROVEN_ANNOTATION_PROVENANCE" for issue in result.issues),
                     "source_unproven_mapping_annotation_files": len({issue["file"] for issue in result.issues
                         if issue["category"] == "UNPROVEN_ANNOTATION_PROVENANCE"})}
    return result
