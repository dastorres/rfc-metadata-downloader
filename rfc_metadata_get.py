"""Download SAP metadata through an HTTPS endpoint that forwards RFC XML."""

import argparse
import base64
from collections import defaultdict
from getpass import getpass
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET


RFC_NS = "urn:sap-com:document:sap:rfc:functions"
DEFAULT_RFC_METHOD = "RFC_GET_FUNCTION_INTERFACE"
RFC_METHODS = (DEFAULT_RFC_METHOD, "RFC_METADATA_GET")
XSD_NS = "http://www.w3.org/2001/XMLSchema"
ET.register_namespace("xs", XSD_NS)
ET.register_namespace("rfc", RFC_NS)
MAX_RESPONSE = 32 * 1024 * 1024


class MetadataError(Exception):
    pass


def load_env(path=None):
    """Load literal KEY=value settings without overriding the process environment."""
    path = path or Path(__file__).with_name(".env")
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", key):
            raise MetadataError(f"Invalid .env setting on line {number}.")
        if value.startswith(("'", '"')):
            if len(value) < 2 or value[-1] != value[0]:
                raise MetadataError(f"Invalid .env quoting on line {number}.")
            value = value[1:-1]
        os.environ.setdefault(key, value)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SapClient:
    def __init__(self, url, user, password, timeout):
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or parts.username is not None:
            raise MetadataError("Provide an HTTPS URL without embedded credentials.")
        token = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
        self.url = url
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/xml; charset=utf-8",
            "Accept": "application/xml, text/xml",
        }
        self.timeout = timeout
        self.opener = build_opener(NoRedirect())
        self.ddic_cache = {}

    def call(self, function, **parameters):
        payload = ET.Element(f"{{{RFC_NS}}}{function}")
        for name, value in parameters.items():
            parameter = ET.SubElement(payload, name)
            if isinstance(value, str):
                parameter.text = value
            else:
                for row in value:
                    item = ET.SubElement(parameter, "item")
                    for field, content in row.items():
                        ET.SubElement(item, field).text = content
        request = Request(
            self.url, ET.tostring(payload, encoding="utf-8", xml_declaration=True),
            headers=self.headers, method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                data = response.read(MAX_RESPONSE + 1)
        except HTTPError as exc:
            hints = {
                401: "check the credentials and iFlow authentication method",
                403: "check endpoint permissions",
                404: "check the URL and iFlow deployment",
            }
            hint = hints.get(exc.code, "check the endpoint and iFlow monitor")
            raise MetadataError(f"{function}: HTTP {exc.code}. {hint}.") from exc
        except (URLError, OSError) as exc:
            raise MetadataError(f"{function}: endpoint connection failed ({exc}).") from exc
        if len(data) > MAX_RESPONSE:
            raise MetadataError(f"{function}: response exceeds 32 MiB.")
        try:
            root = ET.fromstring(data)
        except ET.ParseError as exc:
            raise MetadataError(f"{function}: the endpoint did not return valid XML.") from exc
        for node in root.iter():
            node.tag = node.tag.rsplit("}", 1)[-1]
        if root.tag == "Envelope":
            body = root.find("Body")
            if body is None or len(body) != 1:
                raise MetadataError(f"{function}: unexpected SOAP body.")
            root = body[0]
        if root.tag == "Fault":
            code = root.findtext("faultcode") or root.findtext("Code/Value") or "SOAP"
            raise MetadataError(f"{function}: fault {code}. Check the iFlow monitor.")
        if root.tag != f"{function}.Response":
            raise MetadataError(
                f"{function}: expected {function}.Response, received {root.tag}. "
                "Check the iFlow contract."
            )
        return root

    def fields(self, name, field=""):
        key = (name, field)
        if key not in self.ddic_cache:
            params = {"TABNAME": name, "ALL_TYPES": "X"}
            if field:
                params["LFIELDNAME"] = field
            result = self.call("DDIF_FIELDINFO_GET", **params)
            self.ddic_cache[key] = rows(result, "DFIES_TAB")
        return self.ddic_cache[key]


def values(node):
    return {child.tag: (child.text or "").strip() for child in node}


def rows(root, name):
    table = root.find(name)
    if table is None:
        raise MetadataError(f"{root.tag}: table {name} is missing from the response.")
    return [values(row) for row in table]


def xs(parent, tag, **attributes):
    return ET.SubElement(parent, f"{{{XSD_NS}}}{tag}", attributes)


def xml_name(name):
    name = name.replace("/", "_-")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.-]*", name):
        raise MetadataError(f"SAP name has no supported XML representation: {name!r}.")
    return name


def element(parent, name, **attributes):
    return xs(parent, "element", name=xml_name(name), **attributes)


def sequence(node):
    complex_type = xs(node, "complexType")
    return complex_type, xs(complex_type, "sequence")


def scalar(node, meta, *, idoc=False):
    abap = meta.get("EXID") or meta.get("INTTYPE")
    datatype = meta.get("DATATYPE")
    numeric = {
        "I": "int", "b": "unsignedByte", "s": "short", "8": "long",
        "P": "decimal", "F": "double", "a": "double", "e": "double",
        "X": "base64Binary", "y": "base64Binary",
    }
    if not abap:
        abap = {
            "INT1": "b", "INT2": "s", "INT4": "I", "INT8": "8",
            "DEC": "P", "CURR": "P", "QUAN": "P", "FLTP": "F",
            "RAW": "X", "RSTR": "y",
        }.get(datatype)
    base = "string" if idoc else numeric.get(abap, "string")
    length = int(meta.get("EXTLEN" if idoc else "LENG") or 0)
    if abap == "D" or datatype == "DATS":
        length = length if idoc else max(length, 10)
    elif abap == "T" or datatype == "TIMS":
        length = length if idoc else max(length, 8)
    decimals = int(meta.get("DECIMALS") or 0)
    if base == "string" and length > 0:
        restriction = xs(xs(node, "simpleType"), "restriction", base="xs:string")
        xs(restriction, "maxLength", value=str(length))
    elif abap == "P" and not idoc:
        restriction = xs(xs(node, "simpleType"), "restriction", base="xs:decimal")
        xs(restriction, "fractionDigits", value=str(decimals))
    else:
        node.set("type", f"xs:{base}")


def structure(client, node, name, stack):
    if not name or name in stack:
        raise MetadataError(f"Missing or recursive DDIC structure: {name!r}.")
    fields = client.fields(name)
    if not fields:
        raise MetadataError(f"No DDIC fields returned for {name}.")
    _, content = sequence(node)
    for field in sorted(fields, key=lambda row: int(row.get("POSITION") or 0)):
        field_name = field["FIELDNAME"]
        if field_name.startswith("."):
            continue
        child = element(content, field_name, minOccurs="0")
        rfc_type(client, child, field, stack + (name,))


def rfc_type(client, node, meta, stack=()):
    code = meta.get("EXID") or meta.get("INTTYPE")
    ddic_type = meta.get("TABNAME") if "PARAMETER" in meta else (
        meta.get("PRECFIELD") or meta.get("ROLLNAME")
    )
    if code == "h":
        result = client.call("DDIF_TTYP_GET", NAME=ddic_type or "")
        definition = result.find("DD40V_WA")
        if definition is None:
            raise MetadataError(f"Missing definition for table type {ddic_type!r}.")
        line = values(definition)
        _, entries = sequence(node)
        item = element(entries, "item", minOccurs="0", maxOccurs="unbounded")
        if line.get("ROWKIND") == "S":
            structure(client, item, line.get("ROWTYPE"), stack)
        elif line.get("ROWKIND") in ("", "E"):
            if not line.get("DATATYPE"):
                raise MetadataError(f"Missing row type for {ddic_type!r}.")
            scalar(item, line)
        else:
            raise MetadataError(f"Unsupported row type for {ddic_type!r}.")
    elif meta.get("PARAMCLASS") == "T":
        _, entries = sequence(node)
        item = element(entries, "item", minOccurs="0", maxOccurs="unbounded")
        structure(client, item, ddic_type, stack)
    elif code in ("u", "v"):
        structure(client, node, ddic_type, stack)
    elif code in ("r", "l", "j", "k"):
        raise MetadataError(f"ABAP type {code!r} has no supported RFC XML representation.")
    else:
        scalar(node, meta)


def rfc_parameters(client, name, method):
    if method == DEFAULT_RFC_METHOD:
        response = client.call(method, FUNCNAME=name)
        if response.findtext("REMOTE_CALL") in ("", " "):
            raise MetadataError(f"{name} is not remote-enabled.")
        return rows(response, "PARAMS"), None

    response = client.call(
        method, DEEP="X", EVALUATE_LINKS="X",
        FUNCTIONNAMES=[{"FUNCTIONNAME": name}],
    )
    for error in response.findall("FUNC_ERRORS/item"):
        details = values(error)
        if details.get("FUNCNAME") in (None, "", name):
            message = details.get("EXCEPTION_TEXT") or details.get("EXCEPTION") or "SAP error"
            raise MetadataError(f"{method}: {name}: {message}")
    parameters = [row for row in rows(response, "PARAMETERS") if row.get("FUNCNAME") == name]
    return parameters, response


class RfcMetadataTypes:
    def __init__(self, response):
        self.types = defaultdict(list)
        for item in response.findall("DATATYPESCONT/item"):
            field = values(item)
            self.types[field["TYPENAME"]].append(field)
        self.links = {}
        for item in response.findall("INDIRECTTYPES/item"):
            link = values(item)
            self.links[link["TABNAME"], link["FIELDNAME"]] = link["FIELDTYPE"]

    def render_parameter(self, node, parameter):
        type_name = parameter.get("TABNAME") or ""
        field_name = parameter.get("FIELDNAME") or ""
        code = parameter.get("EXID")
        linked_type = self.links.get((type_name, field_name))
        if linked_type:
            type_name, field_name = linked_type, ""
        if field_name:
            field = next(
                (row for row in self.types[type_name] if row.get("FIELDNAME") == field_name),
                None,
            )
            if field is not None:
                parameter = {**field, **parameter, "LENG": field.get("INTLEN")}
                type_name = field.get("FIELDTYPE") or ""
            elif code in ("h", "u", "v") or parameter.get("PARAMCLASS") == "T":
                raise MetadataError(f"RFC_METADATA_GET: missing reference {type_name}-{field_name}.")
        self.render(node, parameter, type_name, table=parameter.get("PARAMCLASS") == "T")

    def render(self, node, meta, type_name, stack=(), *, table=False):
        code = meta.get("EXID") or meta.get("INTTYPE")
        if table or code == "h":
            _, entries = sequence(node)
            node = element(entries, "item", minOccurs="0", maxOccurs="unbounded")
        if code in ("r", "l", "j", "k"):
            raise MetadataError(f"ABAP type {code!r} has no supported RFC XML representation.")
        if code not in ("h", "u", "v"):
            scalar(node, meta)
            return

        fields = self.types.get(type_name)
        if not fields or type_name in stack:
            raise MetadataError(f"RFC_METADATA_GET: missing or recursive definition for {type_name!r}.")
        next_stack = stack + (type_name,)
        if len(fields) == 1 and not fields[0].get("FIELDNAME"):
            line = fields[0]
            self.render(node, {**line, "LENG": line.get("INTLEN")}, line.get("FIELDTYPE"), next_stack)
            return
        _, content = sequence(node)
        for field in fields:
            child = element(content, field["FIELDNAME"], minOccurs="0")
            self.render(child, {**field, "LENG": field.get("INTLEN")}, field.get("FIELDTYPE"), next_stack)


def rfc_schema(client, name, method=None):
    if method is not None:
        return build_rfc_schema(client, name, method)
    try:
        return build_rfc_schema(client, name, "RFC_METADATA_GET")
    except (MetadataError, ValueError, KeyError) as first_error:
        print(
            f"RFC_METADATA_GET failed: {first_error}. "
            "Trying RFC_GET_FUNCTION_INTERFACE.",
            file=sys.stderr,
        )
        try:
            return build_rfc_schema(client, name, DEFAULT_RFC_METHOD)
        except (MetadataError, ValueError, KeyError) as second_error:
            raise MetadataError(
                f"Both methods failed. RFC_METADATA_GET: {first_error}. "
                f"RFC_GET_FUNCTION_INTERFACE: {second_error}"
            ) from second_error


def build_rfc_schema(client, name, method):
    parameters, response = rfc_parameters(client, name, method)
    metadata = RfcMetadataTypes(response) if response is not None else None
    parameters_by_name = {}
    for row in parameters:
        key = (row.get("PARAMCLASS"), row.get("PARAMETER"))
        # For structures, the header row has an empty FIELDNAME.
        if key not in parameters_by_name or not row.get("FIELDNAME"):
            parameters_by_name[key] = row
    schema = ET.Element(f"{{{XSD_NS}}}schema", targetNamespace=RFC_NS)
    for suffix, directions in (("", "ICT"), (".Response", "ECT")):
        _, content = sequence(element(schema, name + suffix))
        for direction in directions:
            for (param_class, param_name), row in parameters_by_name.items():
                if param_class != direction:
                    continue
                optional = row.get("OPTIONAL") == "X" or direction == "T"
                child = element(content, param_name, minOccurs="0" if optional else "1")
                if metadata is not None:
                    metadata.render_parameter(child, row)
                    continue
                table, field = row.get("TABNAME"), row.get("FIELDNAME")
                if table and field and row.get("EXID") not in ("h", "u", "v") and direction != "T":
                    fields = client.fields(table, field)
                    match = next((item for item in fields if item.get("FIELDNAME") == field), None)
                    if match is None:
                        raise MetadataError(f"Missing DDIC field {table}-{field}.")
                    row = {**row, **match}
                rfc_type(client, child, row)
    return schema


def idoc_schema(client, name, release, extension, namespace):
    params = {"PI_IDOCTYP": name, "PI_RELEASE": release, "PI_VERSION": "3"}
    if extension:
        params["PI_CIMTYP"] = extension
    response = client.call("IDOCTYPE_READ_COMPLETE", **params)
    segments = rows(response, "PT_SEGMENTS")
    if not segments:
        raise MetadataError(f"No segments returned for {name}.")
    fields = defaultdict(list)
    for field in rows(response, "PT_FIELDS"):
        fields[field["SEGMENTTYP"]].append(field)
    attributes = {"targetNamespace": namespace, "elementFormDefault": "qualified"} if namespace else {}
    schema = ET.Element(f"{{{XSD_NS}}}schema", attributes)
    _, root = sequence(element(schema, extension or name))
    idoc_type, body = sequence(element(root, "IDOC", maxOccurs="unbounded"))
    xs(idoc_type, "attribute", name="BEGIN", fixed="1", use="required")
    control_type, control = sequence(element(body, "EDI_DC40"))
    xs(control_type, "attribute", name="SEGMENT", fixed="1", use="required")
    control_fields = client.fields("EDI_DC40")
    if not control_fields:
        raise MetadataError("No DDIC fields returned for EDI_DC40.")
    for field in sorted(control_fields, key=lambda row: int(row.get("POSITION") or 0)):
        visible_field = element(control, field["FIELDNAME"], minOccurs="0")
        scalar(visible_field, {**field, "EXTLEN": field.get("LENG")}, idoc=True)

    parents = [body]
    for segment in sorted(segments, key=lambda row: int(row["NR"])):
        level = int(segment["HLEVEL"])
        name = segment["SEGMENTTYP"]
        if not 1 <= level <= len(parents) or not fields[name]:
            raise MetadataError(f"Incomplete hierarchy or fields for segment {name}.")
        minimum = int(segment["OCCMIN"])
        maximum = int(segment["OCCMAX"])
        if not 0 <= minimum <= maximum or maximum == 0:
            raise MetadataError(f"Invalid cardinality for segment {name}.")
        node = element(parents[level - 1], name, minOccurs=str(minimum), maxOccurs=str(maximum))
        segment_type, content = sequence(node)
        xs(segment_type, "attribute", name="SEGMENT", fixed="1", use="required")
        for field in sorted(fields[name], key=lambda row: int(row.get("FIELD_POS") or 0)):
            scalar(element(content, field["FIELDNAME"], minOccurs="0"), field, idoc=True)
        parents[level:] = [content]
    return schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    try:
        load_env()
    except (MetadataError, OSError) as exc:
        parser.error(str(exc))
    parser.add_argument("name", help="RFC name or IDoc basic type")
    parser.add_argument("--type", "--tipo", choices=("rfc", "idoc"), default="rfc")
    parser.add_argument(
        "--rfc-method", "--metodo-rfc", type=str.upper, choices=RFC_METHODS,
        help="Force a method. By default, try RFC_METADATA_GET, then RFC_GET_FUNCTION_INTERFACE on failure.",
    )
    parser.add_argument("--url", default=os.getenv("SAP_URL"), help="HTTPS endpoint; also accepts SAP_URL")
    parser.add_argument("--user", "--usuario", default=os.getenv("SAP_USER"), help="Also accepts SAP_USER or a prompt")
    parser.add_argument("--password", "--senha", default=os.getenv("SAP_PASSWORD"), help="Also accepts SAP_PASSWORD or a prompt")
    parser.add_argument("--release", help="SAP release for IDoc metadata, such as 757")
    parser.add_argument("--extension", "--extensao", help="IDoc extension type (CIMTYP)")
    parser.add_argument("--idoc-namespace", "--namespace-idoc", help="IDoc XML namespace, if any")
    parser.add_argument("--output", "--saida", type=Path, help="XSD output file path")
    parser.add_argument("--timeout", type=int, default=30, help="Per-call timeout in seconds")
    args = parser.parse_args()
    if not args.url:
        parser.error("Provide --url or set SAP_URL in .env or the environment")
    name = args.name.strip().upper()
    extension = args.extension.strip().upper() if args.extension else ""
    for value in (name, extension) if extension else (name,):
        if not re.fullmatch(r"(?:/[A-Z0-9_]+/)?[A-Z0-9_]+", value) or len(value) > 30:
            parser.error(f"Invalid SAP name: {value!r}")
    if args.type == "idoc" and not args.release:
        parser.error("--release is required for IDoc")
    if args.type == "idoc" and args.rfc_method:
        parser.error("--rfc-method is only valid with --type rfc")
    if args.timeout <= 0:
        parser.error("--timeout must be greater than zero")

    try:
        user = args.user or input("SAP username: ").strip()
        password = args.password or getpass("SAP password: ")
        if not user or ":" in user or not password:
            raise MetadataError("Provide a valid username and password for Basic Authentication.")
        client = SapClient(args.url, user, password, args.timeout)
        if args.type == "rfc":
            schema = rfc_schema(client, name, args.rfc_method)
        else:
            schema = idoc_schema(client, name, args.release, extension, args.idoc_namespace)
        destination = args.output or Path(f"{xml_name(extension or name)}.xsd")
        ET.indent(schema, space="  ")
        ET.ElementTree(schema).write(destination, encoding="utf-8", xml_declaration=True)
    except (MetadataError, OSError, ValueError, KeyError, EOFError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"XSD generated at {destination.resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
