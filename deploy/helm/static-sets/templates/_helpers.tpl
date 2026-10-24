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

{{/* The manager DaemonSet's selector labels: distinct from the controller's. */}}
{{- define "static-sets.managerSelectorLabels" -}}
app.kubernetes.io/name: {{ include "static-sets.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: node-manager
{{- end -}}

{{- define "static-sets.serviceAccountName" -}}
{{- printf "%s-controller" (include "static-sets.fullname" .) -}}
{{- end -}}

{{/* The ServiceAccount the manager DaemonSet runs as. */}}
{{- define "static-sets.managerServiceAccountName" -}}
{{- default (printf "%s-manager" (include "static-sets.fullname" .)) .Values.manager.serviceAccountName -}}
{{- end -}}

{{/* The manager image, defaulting to the controller's. */}}
{{- define "static-sets.managerImage" -}}
{{- printf "%s:%s" (default .Values.image.repository .Values.manager.image.repository) (default .Values.image.tag .Values.manager.image.tag) -}}
{{- end -}}

{{- define "static-sets.managerImagePullPolicy" -}}
{{- default .Values.image.pullPolicy .Values.manager.image.pullPolicy -}}
{{- end -}}

{{/* The TLS secret cert-manager fills for the admission webhook. */}}
{{- define "static-sets.webhookSecretName" -}}
{{- printf "%s-webhook-cert" (include "static-sets.fullname" .) -}}
{{- end -}}

{{/* The name of the webhook configurations kopf keeps in sync. */}}
{{- define "static-sets.webhookManagedName" -}}
{{- printf "%s-webhooks" (include "static-sets.fullname" .) -}}
{{- end -}}
