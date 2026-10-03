"""Render Envoy transport bindings. Model selection belongs exclusively to SR."""
from urllib.parse import urlsplit


def socket(host, port):
    return {"socket_address": {"address": host, "port_value": port}}


def cluster(name, host, port, *, http2=False, tls=False):
    result = {
        "name": name, "connect_timeout": "5s", "type": "STRICT_DNS",
        "dns_lookup_family": "V4_ONLY", "lb_policy": "ROUND_ROBIN",
        "load_assignment": {"cluster_name": name, "endpoints": [{"lb_endpoints": [
            {"endpoint": {"address": socket(host, port)}}]}]},
    }
    if http2:
        result["http2_protocol_options"] = {}
    if tls:
        result["transport_socket"] = {"name": "envoy.transport_sockets.tls", "typed_config": {
            "@type": "type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.UpstreamTlsContext",
            "sni": host, "common_tls_context": {"validation_context": {
                "trusted_ca": {"filename": "/etc/ssl/certs/ca-certificates.crt"},
                "match_typed_subject_alt_names": [{"san_type": "DNS", "matcher": {"exact": host}}],
            }}}}
    return result


def render(models):
    disabled = {"envoy.filters.http.ext_proc": {
        "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_proc.v3.ExtProcPerRoute",
        "disabled": True,
    }}
    routes = [{"match": {"path": "/health"}, "direct_response": {
        "status": 200, "body": {"inline_string": "ok"}}, "typed_per_filter_config": disabled}]
    clusters = [cluster("sr-extproc", "router", 50051, http2=True)]
    for index, model in enumerate(models):
        timeout = model.get("request_timeout_seconds", 120)
        if type(timeout) is not int or not 1 <= timeout <= 7200:
            raise ValueError("request_timeout_seconds must be an integer between 1 and 7200")
        address = urlsplit(model["base_url"])
        name = f"backend-{index}"
        upstream = cluster(name, address.hostname, address.port or (443 if address.scheme == "https" else 80), tls=address.scheme == "https")
        idle = model.get("upstream_idle_timeout_seconds")
        if idle is not None:
            if type(idle) is not int or not 1 <= idle <= 3600:
                raise ValueError("upstream_idle_timeout_seconds must be an integer between 1 and 3600")
            upstream["typed_extension_protocol_options"] = {
                "envoy.extensions.upstreams.http.v3.HttpProtocolOptions": {
                    "@type": "type.googleapis.com/envoy.extensions.upstreams.http.v3.HttpProtocolOptions",
                    "common_http_protocol_options": {"idle_timeout": f"{idle}s"},
                    "explicit_http_config": {"http_protocol_options": {}},
                }}
        clusters.append(upstream)
        routes.append({"match": {"prefix": "/", "headers": [{
            "name": "x-selected-model", "string_match": {"exact": model["name"]}}]},
            "route": {"cluster": name, "timeout": f"{timeout}s", "idle_timeout": f"{timeout}s", "host_rewrite_literal": address.netloc}})
    routes.append({"match": {"prefix": "/"}, "direct_response": {
        "status": 503, "body": {"inline_string": "SR did not select a backend"}}})
    auth = '''local f = assert(io.open("/run/secrets/GATEWAY_TOKEN", "r"))
local token = f:read("*a"):gsub("%s+$", "")
f:close()
assert(#token > 0, "empty gateway token")
function envoy_on_request(h)
  local untrusted = {}
  for name,_ in pairs(h:headers()) do
    if name == "x-selected-model" or name:sub(1,6) == "x-vsr-" or name:sub(1,8) == "x-authz-" then
      table.insert(untrusted, name)
    end
  end
  for _,name in ipairs(untrusted) do
    h:headers():remove(name)
  end
  if h:headers():get(":path") == "/health" then return end
  if h:headers():get("authorization") ~= "Bearer " .. token then
    h:respond({[":status"]="401",["content-type"]="application/json"}, '{"error":{"code":"unauthorized","message":"Invalid gateway token"}}')
    return
  end
end
'''
    hcm = {
        "@type": "type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager",
        "stat_prefix": "inference", "generate_request_id": True,
        "preserve_external_request_id": True, "always_set_request_id_in_response": True,
        "stream_idle_timeout": "120s", "request_timeout": "130s",
        "upgrade_configs": [{"upgrade_type": "websocket"}],
        "route_config": {"name": "inference", "virtual_hosts": [{"name": "inference", "domains": ["*"], "routes": routes}]},
        "http_filters": [
            {"name": "envoy.filters.http.lua", "typed_config": {"@type": "type.googleapis.com/envoy.extensions.filters.http.lua.v3.Lua", "inline_code": auth}},
            {"name": "envoy.filters.http.ext_proc", "typed_config": {
                "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_proc.v3.ExternalProcessor",
                "grpc_service": {"envoy_grpc": {"cluster_name": "sr-extproc"}},
                "failure_mode_allow": False, "allow_mode_override": True,
                "message_timeout": "30s", "max_message_timeout": "120s",
                "processing_mode": {"request_header_mode": "SEND", "response_header_mode": "SEND",
                    "request_body_mode": "BUFFERED", "response_body_mode": "BUFFERED"}}},
            {"name": "envoy.filters.http.router", "typed_config": {"@type": "type.googleapis.com/envoy.extensions.filters.http.router.v3.Router"}},
        ],
        "access_log": [{"name": "envoy.access_loggers.stdout", "typed_config": {
            "@type": "type.googleapis.com/envoy.extensions.access_loggers.stream.v3.StdoutAccessLog",
            "log_format": {"json_format": {
                "time": "%START_TIME%", "request_id": "%REQ(X-REQUEST-ID)%", "status": "%RESPONSE_CODE%",
                "duration_ms": "%DURATION%", "selected_model": "%REQ(X-SELECTED-MODEL)%",
                "upstream_cluster": "%UPSTREAM_CLUSTER%", "upstream_host": "%UPSTREAM_HOST%",
                "path": "%REQ(:PATH)%", "authority": "%REQ(:AUTHORITY)%",
                "decision": "%RESP(X-VSR-SELECTED-DECISION)%", "recipe": "%RESP(X-VSR-SELECTED-RECIPE)%",
                "extproc": "%FILTER_STATE(envoy.filters.http.ext_proc:TYPED)%",
                "extproc_request_body_calls": "%FILTER_STATE(envoy.filters.http.ext_proc:FIELD:request_body_call_count)%",
                "extproc_response_body_calls": "%FILTER_STATE(envoy.filters.http.ext_proc:FIELD:response_body_call_count)%",
                "upgrade": "%REQ(UPGRADE)%",
                "flags": "%RESPONSE_FLAGS%", "routing_ms": "%RESP(X-VSR-ROUTING-LATENCY-MS)%",
            }}}}],
    }
    return {"admin": {"address": socket("127.0.0.1", 9901)}, "static_resources": {
        "clusters": clusters, "listeners": [{"name": "inference", "address": socket("0.0.0.0", 8000),
        "per_connection_buffer_limit_bytes": 16777216,
        "filter_chains": [{"filters": [{"name": "envoy.filters.network.http_connection_manager", "typed_config": hcm}]}]}]}}
