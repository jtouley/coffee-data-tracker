{#- Run first (on-run-start), so a run aimed anywhere else never writes a table: the private database is only ever
    build/importers.duckdb, relative to the working directory (importers.DB; importers.build checks the same file
    isn't a public database, a link or inside a public folder), and holds nothing but this project's own models. A
    COFFEEDATA_IMPORTERS_DB naming the main build's database, the served release's, a public folder or any other
    file stops the run here. -#}
{% macro refuse_unless_private_target() %}
    {%- set fixed = 'build/importers.duckdb' -%}
    {%- if target.path != fixed -%}
        {{ exceptions.raise_compiler_error(
            "COFFEEDATA_IMPORTERS_DB is " ~ target.path ~ ": the importers project builds only into " ~ fixed
            ~ ", never the main build's, the served release's or a public folder's database"
        ) }}
    {%- endif -%}
    {%- if execute -%}
        {%- set own = [] -%}
        {%- for node in graph.nodes.values() if node.resource_type == 'model' and node.package_name == project_name -%}
            {%- do own.append(node.name) -%}
        {%- endfor -%}
        {%- set found = run_query("select table_name from information_schema.tables") -%}
        {%- set foreign = [] -%}
        {%- for row in found.rows if row[0] not in own and not row[0].endswith('__dbt_tmp') -%}
            {%- do foreign.append(row[0]) -%}
        {%- endfor -%}
        {%- if foreign -%}
            {{ exceptions.raise_compiler_error(
                fixed ~ " holds tables the importers project doesn't own (" ~ foreign | join(', ') ~ "): refusing"
            ) }}
        {%- endif -%}
    {%- endif -%}
{% endmacro %}
