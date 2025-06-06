// main.js - Основной файл JavaScript для интерфейса Deeray TG BOT

document.addEventListener('DOMContentLoaded', function() {
    // Обработка наведения на логотип
    const logoContainer = document.querySelector('.logo-container');
    const logoImage = document.querySelector('.logo-image');
    
    if (logoContainer && logoImage) {
        // Логотип меняется через CSS (content: url)
    }

    // Автоматическое скрытие flash-сообщений через 5 секунд
const flashMessages = document.querySelectorAll('.flash-messages .alert');

flashMessages.forEach(message => {
    setTimeout(() => {
        message.style.opacity = '0';
        message.style.transition = 'opacity 0.5s';
        
        setTimeout(() => {
            message.remove();
        }, 500);
    }, 5000);
});

    // Подтверждение на кнопках удаления (если не использовать onsubmit в шаблоне)
    const deleteForms = document.querySelectorAll('form[action*="delete"]');
    
    deleteForms.forEach(form => {
        if (!form.hasAttribute('onsubmit')) {
            form.addEventListener('submit', function(e) {
                const confirmed = confirm('Вы уверены, что хотите выполнить это действие?');
                
                if (!confirmed) {
                    e.preventDefault();
                }
            });
        }
    });

    // Обработка радио-кнопок в форме добавления пользователя
    const radioLabels = document.querySelectorAll('.radio-label');
    
    radioLabels.forEach(label => {
        label.addEventListener('click', function() {
            // Сначала сбрасываем все классы
            radioLabels.forEach(l => {
                l.classList.remove('radio-active');
                l.classList.remove('radio-inactive');
            });

            // Устанавливаем соответствующий класс
            const input = this.querySelector('input');
            if (input) {
                if (input.value === 'enable') {
                    this.classList.add('radio-active');
                } else {
                    this.classList.add('radio-inactive');
                }

                // Устанавливаем значение радио-кнопки
                input.checked = true;
            }
        });
    });

    // Инициализация активной радио-кнопки при загрузке
    radioLabels.forEach(label => {
        const input = label.querySelector('input');
        
        if (input && input.checked) {
            if (input.value === 'enable') {
                label.classList.add('radio-active');
            } else {
                label.classList.add('radio-inactive');
            }
        }
    });

    // Обработка взаимодействия с модальными окнами
    const modalTriggers = document.querySelectorAll('[data-toggle="modal"]');
    const modalCloses = document.querySelectorAll('.close, [data-dismiss="modal"]');
    
    if (modalTriggers.length > 0) {
        modalTriggers.forEach(trigger => {
            trigger.addEventListener('click', function() {
                const modalId = this.getAttribute('data-target');
                const modal = document.querySelector(modalId);
                
                if (modal) {
                    modal.style.display = 'block';
                }
            });
        });
    }
    
    if (modalCloses.length > 0) {
        modalCloses.forEach(close => {
            close.addEventListener('click', function() {
                const modal = this.closest('.modal');
                
                if (modal) {
                    modal.style.display = 'none';
                }
            });
        });
    }
    
    // Закрытие модального окна при клике вне его области
    const modals = document.querySelectorAll('.modal');
    
    if (modals.length > 0) {
        modals.forEach(modal => {
            modal.addEventListener('click', function(e) {
                if (e.target === this) {
                    this.style.display = 'none';
                }
            });
        });
    }

    // Инициализация универсального поиска
    // setupUniversalSearch();
    
// Обработка обновления статуса бота
const refreshStatusBtn = document.getElementById('refresh-status');
if (refreshStatusBtn) {
    refreshStatusBtn.addEventListener('click', function(e) {
        e.preventDefault();
        
        // Show loading state
        this.innerHTML = '<span class="spinner-border spinner-border-sm" role="status" aria-hidden="true"></span> Обновление...';
        this.disabled = true;
        
        // Create a hidden iframe to load the status without triggering errors
        const frameName = 'status-frame-' + Date.now();
        const iframe = document.createElement('iframe');
        iframe.style.display = 'none';
        iframe.name = frameName;
        document.body.appendChild(iframe);
        
        // Create a form to submit to the iframe
        const form = document.createElement('form');
        form.action = '/api/bot-status';
        form.method = 'GET';
        form.target = frameName;
        document.body.appendChild(form);
        
        // Set timeout to reload page after 2 seconds regardless of outcome
        setTimeout(() => {
            window.location.reload();
        }, 2000);
        
        // Submit the form to the hidden iframe
        form.submit();
        
        // Clean up
        setTimeout(() => {
            if (document.body.contains(iframe)) document.body.removeChild(iframe);
            if (document.body.contains(form)) document.body.removeChild(form);
        }, 1000);
    });
} 
// Настройка функциональности SQL-консоли
    setupSqlConsole();
});

// Глобальная функция для редактирования темы
function editSubject(subject) {
    const oldSubjectInput = document.getElementById('oldSubject');
    const newSubjectInput = document.getElementById('newSubject');
    
    if (oldSubjectInput && newSubjectInput) {
        oldSubjectInput.value = subject;
        newSubjectInput.value = subject;
        
        const modal = document.getElementById('editSubjectModal');
        
        if (modal) {
            modal.style.display = 'block';
        }
    }
}

// Функция для переключения отображения столбцов таблицы
function toggleTableColumns(tableName) {
    const columnsDiv = document.getElementById(`columns-${tableName}`);
    if (!columnsDiv) return;
    
    const toggleIcon = columnsDiv.previousElementSibling.querySelector('.toggle-icon');
    
    if (columnsDiv.style.display === 'none' || !columnsDiv.style.display) {
        columnsDiv.style.display = 'block';
        if (toggleIcon) toggleIcon.textContent = '▲';
    } else {
        columnsDiv.style.display = 'none';
        if (toggleIcon) toggleIcon.textContent = '▼';
    }
}

// Debounce функция для оптимизации поиска
function debounce(func, wait) {
    let timeout;
    return function(...args) {
        const context = this;
        clearTimeout(timeout);
        timeout = setTimeout(() => func.apply(context, args), wait);
    };
}

// Настройка функциональности Universal Search для всех таблиц
function setupUniversalSearch() {
    // Находим все поля поиска с соответствующим классом
    const searchInputs = document.querySelectorAll('.search-input');
    
    searchInputs.forEach(searchInput => {
        const searchContainer = searchInput.closest('.search-container');
        if (!searchContainer) return;
        
        // Определяем, является ли поиск серверным или клиентским
        const isServerSide = searchContainer.classList.contains('server-side');
        
        // Находим таблицу, связанную с этим полем поиска
        let targetTable = null;
        const targetSelector = searchContainer.getAttribute('data-target') || 'table';
        
        // Ищем таблицу в ближайших элементах
        let currentElement = searchContainer;
        while (currentElement && !targetTable) {
            const nextElement = currentElement.nextElementSibling;
            if (nextElement) {
                targetTable = nextElement.querySelector(targetSelector);
                if (!targetTable && nextElement.matches(targetSelector)) {
                    targetTable = nextElement;
                }
            }
            currentElement = nextElement;
            if (!currentElement) break;
        }
        
        // Если таблица не найдена через nextElementSibling, ищем через querySelector
        if (!targetTable) {
            targetTable = document.querySelector(targetSelector);
        }
        
        if (!targetTable && !isServerSide) return; // Если таблица не найдена и поиск не серверный, пропускаем
        
        // Находим или создаём элементы управления
        const searchBtn = searchContainer.querySelector('.search-btn');
        let clearBtn = searchContainer.querySelector('.clear-btn');
        let resultsCounter = searchContainer.querySelector('.results-counter');
        
        // Если нет кнопки очистки, создаем её
        if (!clearBtn) {
            clearBtn = document.createElement('button');
            clearBtn.type = 'button';
            clearBtn.className = 'btn clear-btn';
            clearBtn.textContent = 'Очистить';
            if (searchBtn) {
                searchBtn.after(clearBtn);
            } else {
                searchInput.after(clearBtn);
            }
        }

        // Создаем счетчик результатов, если его нет и поиск клиентский
        if (!resultsCounter && !isServerSide) {
            resultsCounter = document.createElement('div');
            resultsCounter.className = 'results-counter';
            searchContainer.appendChild(resultsCounter);
            
            // Инициализируем счетчик
            if (targetTable) {
                const totalRows = targetTable.querySelectorAll('tbody tr').length;
                resultsCounter.textContent = `Найдено: ${totalRows}`;
            }
        }
        
        // Функция для фильтрации таблицы
// Функция для фильтрации таблицы
const filterTable = function() {
    // Если поиск серверный
    if (isServerSide) {
        const form = searchInput.closest('form');
        if (form) {
            // Отправляем форму
            form.submit();
        }
        return;
    }
    
    // Если таблица не найдена для клиентского поиска
    if (!targetTable) return;
    
    // Клиентский поиск
    const searchText = searchInput.value.toLowerCase().trim();
    const rows = targetTable.querySelectorAll('tbody tr');
    let visibleCount = 0;
    let hasResults = false;
    
    rows.forEach(row => {
        let found = false;
        const cells = row.querySelectorAll('td');
        
        cells.forEach(cell => {
            if (cell.textContent.toLowerCase().includes(searchText)) {
                found = true;
            }
        });
        
        // Показываем/скрываем строку
        row.style.display = found ? '' : 'none';
        if (found) {
            visibleCount++;
            hasResults = true;
        }
    });
    
    // Обновляем счетчик результатов
    if (resultsCounter) {
        resultsCounter.textContent = `Найдено: ${visibleCount}`;
    }
    
    // Показываем сообщение, если ничего не найдено
    let noResultsMsg = targetTable.nextElementSibling;
    if (!noResultsMsg || !noResultsMsg.classList.contains('no-results-message')) {
        noResultsMsg = document.createElement('div');
        noResultsMsg.className = 'no-results-message';
        targetTable.after(noResultsMsg);
    }
    
    if (!hasResults && searchText) {
        noResultsMsg.textContent = 'Ничего не найдено';
        noResultsMsg.style.display = 'block';
    } else {
        noResultsMsg.style.display = 'none';
    }
};
        
        // Оптимизированная версия функции поиска с debounce
        const debouncedFilter = debounce(filterTable, 300);
        
        // Обработка события ввода в поле поиска
        searchInput.addEventListener('input', function() {
            debouncedFilter();
        });
        
        // Обработка нажатия Enter в поле поиска
        searchInput.addEventListener('keydown', function(e) {
            if (e.key === 'Enter') {
                e.preventDefault();
                filterTable(); // Немедленно запускаем поиск при нажатии Enter
            }
        });
        
        // Обработка клика на кнопку поиска
        if (searchBtn) {
            searchBtn.addEventListener('click', function(e) {
                e.preventDefault();
                filterTable();
            });
        }
        
        // Обработка клика на кнопку очистки
        clearBtn.addEventListener('click', function(e) {
            e.preventDefault();
            searchInput.value = '';
            
            if (isServerSide) {
                // Если поиск серверный - отправляем форму с пустым значением
                const form = searchInput.closest('form');
                if (form) form.submit();
                return;
            }
            
            // Для клиентского поиска
            if (targetTable) {
                // Показываем все строки
                const rows = targetTable.querySelectorAll('tbody tr');
                rows.forEach(row => {
                    row.style.display = '';
                });
                
                // Обновляем счетчик результатов
                if (resultsCounter) {
                    const totalRows = targetTable.querySelectorAll('tbody tr').length;
                    resultsCounter.textContent = `Найдено: ${totalRows}`;
                }
                
                // Скрываем сообщение об отсутствии результатов
                const noResultsMsg = targetTable.nextElementSibling;
                if (noResultsMsg && noResultsMsg.classList.contains('no-results-message')) {
                    noResultsMsg.style.display = 'none';
                }
            }
        });
        
        // Запускаем поиск при загрузке, если в поле есть значение
        if (searchInput.value.trim()) {
            filterTable();
        }
    });
    
    // Обработка серверной пагинации для клиентского поиска
    const paginationLinks = document.querySelectorAll('.pagination-item');
    paginationLinks.forEach(link => {
        link.addEventListener('click', function(e) {
            const searchInput = document.querySelector('.search-input');
            if (searchInput && searchInput.value.trim() !== '') {
                // Проверяем, активен ли клиентский поиск
                const searchContainer = searchInput.closest('.search-container');
                if (searchContainer && !searchContainer.classList.contains('server-side')) {
                    // Если есть скрытая форма для серверного поиска
                    const serverSearchInput = document.getElementById('server-search-input');
                    if (serverSearchInput) {
                        e.preventDefault();
                        // Используем серверную форму для перехода
                        serverSearchInput.value = searchInput.value;
                        const pageMatch = this.href.match(/page=(\d+)/);
                        if (pageMatch && document.getElementById('server-page-input')) {
                            document.getElementById('server-page-input').value = pageMatch[1];
                        }
                        document.getElementById('server-search-form').submit();
                    }
                }
            }
        });
    });
}

// Настройка функциональности SQL-консоли
function setupSqlConsole() {
    // Разворачиваемая структура БД
    const dbTableHeaders = document.querySelectorAll('.db-table-header');
    if (dbTableHeaders.length > 0) {
        dbTableHeaders.forEach(header => {
            header.addEventListener('click', function() {
                const tableName = this.querySelector('.table-name').textContent;
                toggleTableColumns(tableName);
            });
        });
    }
    
    // Клик на колонку для добавления в запрос
    const dbColumns = document.querySelectorAll('.db-column');
    const sqlEditor = document.querySelector('.sql-editor');
    
    if (dbColumns.length > 0 && sqlEditor) {
        dbColumns.forEach(column => {
            column.addEventListener('click', function() {
                const table = this.getAttribute('data-table');
                const columnName = this.getAttribute('data-column');
                
                if (columnName) {
                    // Если выбрана колонка, то добавляем её в запрос
                    const currentQuery = sqlEditor.value.trim();
                    if (currentQuery === '') {
                        sqlEditor.value = `SELECT ${columnName} FROM ${table};`;
                    } else {
                        // Проверяем, содержит ли запрос уже название таблицы
                        if (!currentQuery.toUpperCase().includes(table.toUpperCase())) {
                            // Если нет, просто добавляем колонку через запятую
                            if (currentQuery.includes('SELECT') && !currentQuery.includes('FROM')) {
                                sqlEditor.value = currentQuery.replace('SELECT', `SELECT ${columnName},`);
                            }
                        }
                    }
                }
            });
        });
    }
    
    // Обработка клика на готовые запросы
    const queryItems = document.querySelectorAll('.query-item');
    
    if (queryItems.length > 0 && sqlEditor) {
        queryItems.forEach(item => {
            item.addEventListener('click', function() {
                const query = this.getAttribute('data-query');
                if (query) {
                    sqlEditor.value = query;
                }
            });
        });
    }
    
    // Обработка Ctrl+Enter для SQL запросов
    if (sqlEditor) {
        sqlEditor.addEventListener('keydown', function(e) {
            if (e.ctrlKey && (e.key === 'Enter' || e.keyCode === 13)) {
                e.preventDefault();
                const form = document.querySelector('form#sql-form');
                if (form) form.submit();
            }
        });
    }
}