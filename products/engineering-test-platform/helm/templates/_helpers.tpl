{{/* Chart name, overridable by nameOverride. */}}
{{- define "engineering-test-platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/* Fully qualified release name, used for the Deployment and Service. */}}
{{- define "engineering-test-platform.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := include "engineering-test-platform.name" . -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "engineering-test-platform.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{/*
Selector labels. These are immutable on a Deployment, so they deliberately
exclude version and chart: a chart upgrade must never change the selector.
*/}}
{{- define "engineering-test-platform.selectorLabels" -}}
app.kubernetes.io/name: {{ include "engineering-test-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Full label set for metadata. */}}
{{- define "engineering-test-platform.labels" -}}
helm.sh/chart: {{ include "engineering-test-platform.chart" . }}
{{ include "engineering-test-platform.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}
