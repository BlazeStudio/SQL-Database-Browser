import os
import re
import sqlite3
import datetime
from functools import wraps
from markupsafe import Markup

try:
    from flask import (Flask, render_template, request, abort, session,
                       flash, redirect, url_for, make_response, send_file, send_from_directory, jsonify)
except ImportError:
    raise RuntimeError('Unable to import flask module. Install by running '
                       'pip install flask')
try:
    from pygments import formatters, highlight, lexers
except ImportError:
    import warnings
    warnings.warn('pygments library not found.', ImportWarning)
    syntax_highlight = lambda data: '<pre>%s</pre>' % data
else:
    def syntax_highlight(data):
        if not data:
            return ''
        lexer = lexers.get_lexer_by_name('sql')
        formatter = formatters.HtmlFormatter(linenos=False)
        return highlight(data, lexer, formatter)


# CUR_DIR = os.path.realpath(os.path.dirname(__file__))
DEBUG = True
SECRET_KEY = 'sqlite-database-browser-0.1.0'
MAX_RESULT_SIZE = 50
ROWS_PER_PAGE = 20
OUT_FOLDER = 'export_file'

app = Flask(__name__)
app.config.from_object(__name__)

dataset = None

class SqliteTools():

    def __init__(self, filename):
        self.file = filename
        self.db = sqlite3.connect(filename, check_same_thread=False)
        self.cursor = self.db.cursor()

    @property
    def filename(self):
        return self.file

    @property
    def location(self):
        return os.path.abspath(self.file)

    @property
    def size(self):
        return os.path.getsize(self.file)

    @property
    def created(self):
        stat = os.stat(self.file)
        return datetime.datetime.fromtimestamp(stat.st_ctime)

    @property
    def modified(self):
        stat = os.stat(self.file)
        return datetime.datetime.fromtimestamp(stat.st_mtime)

    @property
    def tables(self):
        tables = self.cursor.execute(
            'SELECT name FROM sqlite_master WHERE type="table" ORDER BY name;')
        return set([row[0] for row in tables.fetchall()])

    def get_table(self, table):
        table = self.cursor.execute(
            'SELECT * FROM %s;' % (table,)).fetchall()
        return table

    def table_sql(self, table):
        sql = self.cursor.execute('SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type = ?',
                                  [table, 'table']).fetchone()[0]
        return sql

    def update_cell(self,sql, data):
        self.cursor.execute(sql, data)
        self.db.commit()
    def get_table_info(self, table):
        info = self.cursor.execute(
            "PRAGMA table_info('%s')" % table).fetchall()
        return info

    def get_foreign_keys(self, table):
        return self.cursor.execute("PRAGMA foreign_key_list('%s')" % table).fetchall()

    def get_indexes(self, table):
        return self.cursor.execute("PRAGMA index_list('%s')" % table).fetchall()

    def paginate(self, table, page, paginate_by=20, order=None):
        if page > 0:
            page -= 1
        if order:
            table_page = self.cursor.execute(
                'SELECT * FROM %s ORDER BY %s LIMIT %s OFFSET %s;' % (table, order, paginate_by, page * paginate_by)).fetchall()
        else:
            table_page = self.cursor.execute(
                'SELECT * FROM %s LIMIT %s OFFSET %s;' % (table, paginate_by, page * paginate_by)).fetchall()
        return table_page

    def delete_table(self, table):
        self.cursor.execute("DROP TABLE %s" % table)

    def copy_table(self, old_table, new_table):
        infos = self.get_table_info(old_table)
        old_columns = ','.join([row[1] for row in infos])
        if 'default' in old_columns:
            old_columns = old_columns.replace('default', '"default"')
        infos = self.get_table_info(new_table)
        new_columns = ','.join([row[1] for row in infos])
        sql = 'INSERT INTO %s(%s) SELECT %s FROM %s;' % (
            new_table, new_columns, old_columns, old_table)
        self.cursor.execute(sql)

    def delete_column(self, table, column):
        self.cursor.execute(f"PRAGMA table_info({table})")
        columns_info = self.cursor.fetchall()

        # Формируем список столбцов и их атрибутов для временной таблицы, исключая столбец, который мы хотим удалить
        new_columns = []
        for column_info in columns_info:
            if column_info[1] != column:
                column_name = column_info[1]
                column_type = column_info[2]
                # Добавляем атрибуты PRIMARY KEY, UNIQUE и NOT NULL, если они присутствуют в исходной таблице
                if column_info[3] == 1:
                    column_type += " NOT NULL"
                if column_info[5] == 1:
                    column_type += " PRIMARY KEY"
                if column_info[4] == 1:
                    column_type += " UNIQUE"
                new_columns.append(f"{column_name} {column_type}")

        # Проверяем, остались ли еще столбцы в таблице
        if len(new_columns) == 0:
            return flash('Невозможно удалить последний столбец в таблице', 'danger')

        # Создаем временную таблицу с новой структурой и данными из исходной таблицы
        self.cursor.execute(f"CREATE TABLE temp_{table} ({', '.join(new_columns)})")
        self.cursor.execute(
            f"INSERT INTO temp_{table} SELECT {', '.join([col_info[1] for col_info in columns_info if col_info[1] != column])} FROM {table}")

        # Удаляем исходную таблицу
        self.cursor.execute(f"DROP TABLE {table}")

        # Переименовываем временную таблицу обратно в исходное имя
        self.cursor.execute(f"ALTER TABLE temp_{table} RENAME TO {table}")
        self.db.commit()

        return flash('Столбец "%s" был успешно удалён' % column, 'success')

    def add_column(self, table, column, column_type, unique=True):
        self.cursor.execute(f"SELECT COUNT(*) FROM {table}")
        count = self.cursor.fetchone()[0]
        if (count > 0) and ("AUTOINCREMENT" in column_type):
            flash("AUTOINCREMENT не может быть применён, так как в таблице уже созданы строки", 'danger')
            return False
        columns = [row[1] for row in self.get_table_info(table)]
        if column and column_type:
            # Создаем временную таблицу
            temp_table_name = f"temp_{table}"

            self.cursor.execute(f"PRAGMA table_info({table})")
            source_columns_info = self.cursor.fetchall()

            # Составляем список столбцов и их атрибутов для новой таблицы
            new_columns = [f"{col_info[1]} {' '.join(col_info[2:])}" for col_info in source_columns_info]

            # Создаем новую таблицу с аналогичными столбцами
            self.cursor.execute(f"CREATE TABLE {temp_table_name} ({', '.join(new_columns)})")

            # Переносим данные из исходной таблицы в новую таблицу
            self.cursor.execute(f"INSERT INTO {temp_table_name} SELECT * FROM {table}")


            # self.cursor.execute(f"CREATE TABLE {temp_table_name} AS SELECT * FROM {table}")
            # # Удаляем исходную таблицу
            # self.cursor.execute(f"DROP TABLE {table}")

            # # Создаем новую таблицу с добавленным столбцом и ограничением UNIQUE
            # self.cursor.execute(f"CREATE TABLE {table} ({', '.join(columns + [f'{column} {column_type} UNIQUE' if unique else f'{column} {column_type}'])})")
            #
            # # # Переносим данные из временной таблицы в новую таблицу
            # self.cursor.execute(f"INSERT INTO {table} SELECT *, NULL FROM {temp_table_name}")
            #
            # # Удаляем временную таблицу
            # self.cursor.execute(f"DROP TABLE {temp_table_name}")
            # self.db.commit()

            return True
        else:
            return False


    # def add_column(self, table, column, column_type):
    #     self.cursor.execute(f"SELECT COUNT(*) FROM {table}")
    #     count = self.cursor.fetchone()[0]
    #     if (count > 0) and ("AUTOINCREMENT" in column_type):
    #         return flash("AUTOINCREMENT не может быть применён, так как в таблице уже созданы строки")
    #     columns = [row[1] for row in self.get_table_info(table)]
    #     if column and column not in columns and column_type:
    #         self.cursor.execute('ALTER TABLE %s ADD COLUMN %s INTEGER' % (table, column))
    #         self.cursor.execute(f'ALTER TABLE {table} MODIFY COLUMN {column} NOT NULL')
    #         self.db.commit()
    #         return True
    #     else:
    #         return False

    def add_row(self, table):
        self.cursor.execute(f"PRAGMA table_info({table})")
        columns_info = self.cursor.fetchall()
        values = []
        for column_info in columns_info:
            column_name = column_info[1]
            if column_name == "id" and column_info[5]:  # Проверяем, является ли столбец уникальным ключом и автоинкрементируемым
                # Используем NULL, чтобы база данных сама сгенерировала значение
                values.append('NULL')
            elif column_info[3] == 1:  # Если столбец не позволяет NULL значений
                values.append("''")  # Заполняем пустым символом
            else:
                values.append('NULL')  # Заполняем NULL

        columns_str = ', '.join(column_info[1] for column_info in columns_info)
        values_str = ', '.join(values)
        query = f"INSERT INTO {table} ({columns_str}) VALUES ({values_str})"

        self.cursor.execute(query)
        self.db.commit()

    def rename_cloumn(self, table, rename, rename_to):
        sql = self.cursor.execute('SELECT sql FROM sqlite_master WHERE tbl_name = ? AND type = ?',
                                  [table, 'table']).fetchone()[0]
        self.cursor.execute(
            "ALTER TABLE %s RENAME TO old_%s" % (table, table))
        r = '\\b' + rename + '\\b'
        sql = re.sub(r, rename_to, sql)
        self.cursor.execute(sql)
        self.copy_table("old_%s" % table, table)
        self.delete_table("old_%s" % table)

def require_database(fn):
    @wraps(fn)
    def inner(table, *args, **kwargs):
        if not database:
            return redirect(url_for('index'))
        if table not in dataset.tables:
            abort(404)
        return fn(table, *args, **kwargs)
    return inner



@app.route('/', methods=('GET', 'POST'))
def index():
    if request.method == 'POST':
        global database
        file = request.files.get('sqlite-file')
        if not file or file.filename.split('.')[-1] != 'db':
            database = None
            flash('Не выбрана база данных SQL', 'danger')
        else:
            file.save(file.filename)
            database = file.filename
            return redirect(url_for('index'))
    return render_template('index.html')


@app.route('/<table>', methods=('GET', 'POST'))
@require_database
def table_info(table):
    return render_template(
        'table_structure.html',
        columns=dataset.get_table(table),
        infos=dataset.get_table_info(table),
        table=table,
        # indexes=dataset.get_indexes(table),
        # foreign_keys=dataset.get_foreign_keys(table),
        table_sql=dataset.table_sql(table))


@app.route('/<table>/rename-column', methods=['GET', 'POST'])
@require_database
def rename_column(table):
    rename = request.args.get('rename')
    infos = dataset.get_table_info(table)
    column_names = [row[1] for row in infos]
    if request.method == 'POST':
        new_name = request.form.get('rename_to', '')
        rename = request.form.get('rename', '')
        if new_name and new_name not in column_names:
            dataset.rename_cloumn(table, rename, new_name)
            flash('Столбец "%s" успешно переименован!' % rename, 'success')
        else:
            flash('Название столбца не должно быть пустым или совпадать с другим', 'danger')
        return redirect(url_for('rename_column', table=table))
    return render_template(
        'rename_column.html',
        infos=infos,
        table=table,
        rename=rename,
    )


@app.route('/<table>/delete-column/', methods=['GET', 'POST'])
@require_database
def delete_column(table):
    name = request.args.get('name')
    infos = dataset.get_table_info(table)
    if request.method == 'POST':
        name = request.form.get('name', '')
        if (name == None): flash('Столбец не указан', 'danger')
        else:
            dataset.delete_column(table, name)
        return redirect(url_for('table_info', table=table))
    return render_template(
        'delete_column.html',
        infos=infos,
        table=table,
        name=name)



@app.route('/<table>/add-column/', methods=['GET', 'POST'])
@require_database
def add_column(table):
    column_mapping = ['VARCHAR', 'TEXT', 'INTEGER', 'REAL',
                      'BOOL', 'BLOB', 'DATETIME', 'DATE', 'TIME', 'DECIMAL']
    if request.method == 'POST':
        name = request.form.get('name', '')
        column_type = request.form.get('type', '')
        not_null = 'NOT NULL' if request.form.get('not_null') else ''
        unique = 'UNIQUE' if request.form.get('unique') else ''
        autoincrement = 'AUTOINCREMENT' if request.form.get('autoincrement') else ''
        atr = unique
        print(atr)
        if name and column_type:
            success = dataset.add_column(table, name, f'{column_type} {atr}')
            if success:
                flash('Столбец "%s" был успешно создан' % name, 'success')
            else:
                flash('Столбец с таким именем уже существует', 'danger')
        else:
            flash('Имя и тип не могут быть пустыми', 'danger')
        return redirect(url_for('add_column', table=table))
    return render_template('add_column.html', column_mapping=column_mapping, table=table)

@app.route('/<table>/add-row/', methods=['GET', 'POST'])
@require_database
def add_row(table):
    dataset.add_row(table)
    return redirect(url_for('table_content', table=table))

@app.route('/apply_changes', methods=['POST'])
def apply_changes():
    table = request.form.get('table_name')
    name = request.form.get('columnLabel')
    row = int(request.form.get('rowLabel'))
    new_value = request.form.get('newValue')
    try:
        sql = f"UPDATE {table} SET {name} = ? WHERE rowid = ?"
        dataset.update_cell(sql, (new_value, row))

        return jsonify({'message': 'Данные успешно обновлены в базе данных.'})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/<table>/content/', methods=['GET', 'POST'])
@require_database
def table_content(table):
    columns_count = dataset.get_table(table)
    ordering = request.args.get('ordering')
    rows_per_page = app.config['ROWS_PER_PAGE']
    page = request.args.get('page', 1, type=int)
    if ordering:
        columns = dataset.paginate(
            table, page, paginate_by=rows_per_page, order=ordering)
    else:
        columns = dataset.paginate(
            table=table, page=page, paginate_by=rows_per_page)
    total_pages = (len(columns_count) // rows_per_page) + 1
    previous_page = page - 1
    next_page = page + 1 if page + \
        1 <= total_pages else 0
    return render_template(
        'table_content.html',
        columns=columns,
        ordering=ordering,
        page=page,
        total_pages=total_pages,
        previous_page=previous_page,
        next_page=next_page,
        columns_count=columns_count,
        infos=dataset.get_table_info(table),
        table=table,
    )


@app.route('/<table>/query/', methods=['GET', 'POST'])
@require_database
def table_query(table):
    row_count, error, data, data_description = None, None, None, None
    if request.method == 'POST':
        sql = request.form.get('sql', '')
        try:
            cur = dataset.cursor.execute(sql)
            dataset.db.commit()
        except Exception as exc:
            error = str(exc)
        else:
            data = cur.fetchall()[:app.config['MAX_RESULT_SIZE']]
            data_description = cur.description
            row_count = cur.rowcount
    else:
        if request.args.get('sql'):
            sql = request.args.get('sql')
        else:
            sql = 'SELECT *\nFROM "%s"' % (table)

    return render_template(
        'table_query.html',
        row_count=row_count,
        data=data,
        data_description=data_description,
        table=table,
        sql=sql,
        error=error,
        table_sql=dataset.table_sql(table)
    )


@app.route('/table_create/', methods=['POST'])
def table_create():
    table = request.form.get('table_name', '')
    if not table:
        flash('Введите имя таблицы.', 'danger')
        return redirect(request.referrer)
    dataset.cursor.execute('CREATE TABLE %s(id INTEGER NOT NULL UNIQUE PRIMARY KEY )' % table)
    return redirect(url_for('table_info', table=table))


@app.route('/<table>/delete', methods=['GET', 'POST'])
@require_database
def delete_table(table):
    if request.method == 'POST':
        try:
            dataset.cursor.execute('DROP TABLE %s' % table)
        except Exception as exc:
            flash('Ошибка импортирования файла: %s' % exc, 'danger')
        else:
            flash('Таблица "%s" была успешно удалена.' % table, 'success')
            return redirect(url_for('index'))
    return render_template('delete_table.html', table=table)


@app.route('/close')
def close():
    global database
    global dataset
    dataset = None
    database = None
    return redirect(url_for('index'))


column_re = re.compile('(.+?)\((.+)\)', re.S)
column_split_re = re.compile(r'(?:[^,(]|\([^)]*\))+')


def _format_create_table(sql):
    create_table, column_list = column_re.search(sql).groups()
    columns = ['  %s' % column.strip()
               for column in column_split_re.findall(column_list)
               if column.strip()]
    return '%s (\n%s\n)' % (
        create_table,
        ',\n'.join(columns))


@app.template_filter()
def format_create_table(sql):
    try:
        return _format_create_table(sql)
    except:
        return sql


@app.template_filter('highlight')
def highlight_filter(data):
    return Markup(syntax_highlight(data))


@app.context_processor
def _general():
    return {
        'dataset': dataset,
    }

@app.before_request
def _before_request():
    if database:
        global dataset
        dataset = SqliteTools(database)

def main():
    global database
    database = None
    app.run()

if __name__ == '__main__':
    main()
