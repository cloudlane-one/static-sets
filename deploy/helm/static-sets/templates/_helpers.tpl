{{/*
Shared names and labels.
*/}}

{{- define "static-sets.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "static-sets.fullname" -}}
{{- if .Values.fullnameOverride -}}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- $name := default .Chart.Name .Values.nameOverride -}}
{{- if contains $name .Release.Name -}}
{{- .Release.Name | trunc 63 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{- define "static-sets.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" -}}
{{- end -}}

{{- define "static-sets.labels" -}}
helm.sh/chart: {{ include "static-sets.chart" . }}
{{ include "static-sets.selectorLabels" . }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "static-sets.selectorLabels" -}}
app.kubernetes.io/name: {{ include "static-sets.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "static-sets.serviceAccountName" -}}
{{- printf "%s-controller" (include "static-sets.fullname" .) -}}
{{- end -}}

{{/* The ServiceAccount the installer Pods run as. */}}
{{- define "static-sets.installerServiceAccountName" -}}
{{- default (printf "%s-installer" (include "static-sets.fullname" .)) .Values.installer.serviceAccountName -}}
{{- end -}}

{{/* The namespace the operator watches and creates objects in. */}}
{{- define "static-sets.watchNamespace" -}}
{{- default .Release.Namespace .Values.watchNamespace -}}
{{- end -}}

{{/* The TLS secret cert-manager fills for the admission webhook. */}}
{{- define "static-sets.webhookSecretName" -}}
{{- printf "%s-webhook-cert" (include "static-sets.fullname" .) -}}
{{- end -}}

{{/* The name of the webhook configurations kopf keeps in sync. */}}
{{- define "static-sets.webhookManagedName" -}}
{{- printf "%s-webhooks" (include "static-sets.fullname" .) -}}
{{- end -}}