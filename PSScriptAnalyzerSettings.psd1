# PSScriptAnalyzer settings for scripts/check.ps1 (Error and Warning severities are gates).
@{
    Severity     = @('Error', 'Warning')
    ExcludeRules = @(
        # The launcher and the check script print for a person at a console, on purpose.
        'PSAvoidUsingWriteHost',
        # Empty catches are deliberate "try the next option" fallbacks (probing folders, TLS, Python candidates).
        'PSAvoidUsingEmptyCatchBlock'
    )
}
