{{/*
Expand the name of the chart.
*/}}
{{- define "cuopt-server.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this
(by the DNS naming spec). If release name contains chart name it will be used as
a full name.
*/}}
{{- define "cuopt-server.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "cuopt-server.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "cuopt-server.labels" -}}
helm.sh/chart: {{ include "cuopt-server.chart" . }}
{{ include "cuopt-server.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: gridiron-estate
app.kubernetes.io/component: solver
{{- end }}

{{/*
Selector labels
*/}}
{{- define "cuopt-server.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cuopt-server.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
MCP sidecar selector labels.

Deliberately DIFFERENT from cuopt-server.selectorLabels. The solver Service
selects on name+instance only, so if the MCP pods carried the same pair the
solver Service would start load-balancing solve traffic onto a sidecar that has
no GPU and does not speak the solver's API. Both the name suffix and the
component label keep the two sets disjoint.
*/}}
{{- define "cuopt-server.mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "cuopt-server.name" . }}-mcp
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: mcp
{{- end }}

{{/*
MCP sidecar common labels
*/}}
{{- define "cuopt-server.mcp.labels" -}}
helm.sh/chart: {{ include "cuopt-server.chart" . }}
{{ include "cuopt-server.mcp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
app.kubernetes.io/part-of: gridiron-estate
{{- end }}

{{/*
The in-cluster URL of the solver the MCP sidecar proxies to.
*/}}
{{- define "cuopt-server.mcp.solverUrl" -}}
{{- if .Values.mcp.solverUrl }}
{{- .Values.mcp.solverUrl }}
{{- else }}
{{- printf "http://%s:%v" (include "cuopt-server.fullname" .) .Values.service.port }}
{{- end }}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "cuopt-server.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "cuopt-server.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
