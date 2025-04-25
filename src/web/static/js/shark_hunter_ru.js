/**
 * Улучшенная игра "Охотник за Акулой"
 * Поддержка меняющихся фонов и бонусов
 */
document.addEventListener('DOMContentLoaded', () => {
    // --- DOM элементы ---
    const canvas = document.getElementById('gameCanvas');
    const ctx = canvas ? canvas.getContext('2d') : null;
    const gameArea = document.getElementById('game-area');
    const scoreDisplay = document.getElementById('score-display');
    const comboDisplay = document.getElementById('combo-display');
    const levelDisplay = document.getElementById('level-display');
    const livesDisplay = document.getElementById('lives-display');
    const gameMessageContainer = document.getElementById('game-message-container');
    const gameMessage = document.getElementById('game-message');
    const restartButton = document.getElementById('restart-button');
    const levelUpAnimation = document.getElementById('level-up-animation');
    const loadingScreen = document.getElementById('loading-screen');
    const loadingBar = document.getElementById('loading-bar');
    const loadingText = document.getElementById('loading-text');
    const powerUpIndicator = document.getElementById('powerup-indicator');

    // --- Проверка наличия всех элементов ---
    if (!canvas || !ctx || !gameArea || !scoreDisplay || !comboDisplay) {
        console.error("ОШИБКА: Не найдены основные DOM элементы!");
        if (gameArea) {
            gameArea.innerHTML = "<p style='color:red; padding: 20px;'>Ошибка: Не найдены необходимые элементы для игры.</p>";
        }
        return;
    }

    // --- Константы игры ---
    const BASE_WIDTH = 1000;  // Логическая ширина игры
    const BASE_HEIGHT = 600;  // Логическая высота игры

    // Настройки игрока
    const SHARK_SCALE = 0.8;
    const SHARK_LERP_FACTOR = 0.1;  // Коэффициент плавности движения
    const SHARK_MAX_TILT = Math.PI / 6;  // Максимальный угол наклона
    const SHARK_TILT_SPEED = 0.12;  // Скорость наклона акулы
    const MAGNET_RADIUS = 150;  // Радиус действия магнита
    const IMMUNITY_DURATION = 1500;  // Длительность иммунитета в мс

    // Настройки предметов
    const INITIAL_ITEM_SPEED = 3.0;
    const INITIAL_SPAWN_INTERVAL = 800;
    const ITEM_SIZE = 40;  // Базовый размер предметов
    const INITIAL_OBSTACLE_PROBABILITY = 0.3;  // Вероятность препятствий
    const SPECIAL_PROBABILITY = 0.05;  // Вероятность особых предметов
    const POWERUP_PROBABILITY = 0.03;  // Вероятность бонусов
    const POWERUP_DURATION = 8000;  // Длительность бонусов в мс

    // Настройки комбо и очков
    const COMBO_TIMEOUT = 3000;  // Время до сброса комбо (мс)
    const COMBO_MULTIPLIER_MAX = 10;  // Максимальный множитель комбо

    // Частицы
    const PARTICLE_COUNT_COLLECT = 12;
    const PARTICLE_COUNT_HIT = 25;
    const PARTICLE_LIFESPAN = 1.0;  // В секундах
    const PARTICLE_SPEED = 2.5;

    // Прогресс уровней
    const POINTS_PER_LEVEL = 1000;  // Очков для повышения уровня
    const MAX_LEVEL = 10;

    // Фоны и смена фона
    const BACKGROUND_CHANGE_INTERVAL = 30000;  // 30 секунд между сменой фона
    const BACKGROUND_TRANSITION_DURATION = 2000;  // 2 секунды на переход

    // Пузырьки
    const BUBBLE_COUNT = 15;
    const MIN_BUBBLE_SIZE = 5;
    const MAX_BUBBLE_SIZE = 20;
    const MIN_BUBBLE_SPEED = 1;
    const MAX_BUBBLE_SPEED = 3;

    // --- Состояние игры ---
    let gameState = 'LOADING';
    let prevTime = 0;
    let score = 0;
    let combo = 1;
    let comboTimer = 0;
    let level = 1;
    let lives = 3;
    let itemSpeed = INITIAL_ITEM_SPEED;
    let spawnInterval = INITIAL_SPAWN_INTERVAL;
    let obstacleProbability = INITIAL_OBSTACLE_PROBABILITY;
    let isImmune = false;
    let immuneTimer = 0;
    let gameTime = 0;  // Общее время игры в мс
    let backgroundChangeTimer = 0;  // Таймер для автоматической смены фона
    let gameStats = {
        treasuresCollected: 0,
        obstaclesHit: 0,
        powerUpsCollected: 0,
        maxCombo: 1,
        timeAlive: 0
    };

    // Система бонусов
    let activePowerUps = {
        magnet: { active: false, endTime: 0 },
        shield: { active: false, endTime: 0 },
        star: { active: false, endTime: 0 }  // Звезда = двойные очки
    };

    // Система смены фонов
    let backgroundIndex = 0;
    let backgroundTransition = {
        active: false,
        from: 0,
        to: 0,
        progress: 0,
        duration: BACKGROUND_TRANSITION_DURATION
    };

    // Переменные анимации и рендеринга
    let logicalMousePos = { x: BASE_WIDTH / 2, y: BASE_HEIGHT / 2 };
    let animationFrameId = null;
    let shark = null;
    let items = [];
    let particles = [];
    let bubbles = [];
    let assets = {};
    let assetsLoaded = 0;
    let totalAssets = 0;
    let spawnTimer = 0;

    // --- Переменные масштабирования холста ---
    let scale = 1;  // Масштаб от логических к реальным координатам
    let currentCanvasWidth = BASE_WIDTH;  // Реальный размер холста в пикселях
    let currentCanvasHeight = BASE_HEIGHT;  // Реальный размер холста в пикселях
    let offsetX = 0;  // Смещение для расчета позиции мыши
    let offsetY = 0;  // Смещение для расчета позиции мыши

    // --- Класс загрузчика ресурсов ---
    class AssetLoader {
        constructor(assetMap, onComplete, onProgress) {
            this.assetMap = assetMap;
            this.onComplete = onComplete;
            this.onProgress = onProgress;
            this.assets = {};
            this.totalAssets = Object.keys(this.assetMap).length;
            this.loadedCount = 0;
            this.errors = [];
        }

        load() {
            console.log(`AssetLoader: Начало загрузки ${this.totalAssets} ресурсов.`);
            if (this.totalAssets === 0) {
                this.completeLoading();
                return;
            }

            for (const key in this.assetMap) {
                const elementId = this.assetMap[key];
                const imgElement = document.getElementById(elementId);

                if (!imgElement || !(imgElement instanceof HTMLImageElement)) {
                    console.warn(`AssetLoader: HTMLImageElement с ID "${elementId}" не найден для ключа "${key}". Пропускаем.`);
                    this.assetLoadError(key);
                    continue;
                }

                const img = new Image();
                img.onload = () => this.assetLoaded(key, img);
                img.onerror = () => this.assetLoadError(key, imgElement.src);
                img.src = imgElement.src;

                // Если изображение уже загружено (кэшировано браузером)
                if (img.complete) {
                    this.assetLoaded(key, img);
                }
            }
        }

        assetLoaded(key, img) {
            if (key in this.assets) return; // Предотвращаем дублирование

            this.loadedCount++;
            this.assets[key] = img;
            console.log(`AssetLoader: Загружен "${key}" (${this.loadedCount}/${this.totalAssets})`);
            this.updateProgress();

            if (this.loadedCount >= this.totalAssets) {
                this.completeLoading();
            }
        }

        assetLoadError(key, src = 'N/A') {
            if (this.errors.includes(key)) return; // Предотвращаем дублирование

            this.loadedCount++;
            this.errors.push(key);
            console.warn(`AssetLoader: Не удалось загрузить ресурс "${key}" из источника: ${src}`);
            this.updateProgress();

            if (this.loadedCount >= this.totalAssets) {
                this.completeLoading();
            }
        }

        updateProgress() {
            if (this.onProgress) {
                const progress = (this.loadedCount / this.totalAssets) * 100;
                this.onProgress(this.loadedCount, this.totalAssets, progress);
            }
        }

        completeLoading() {
            console.log("AssetLoader: Загрузка завершена.");

            if (this.errors.length > 0) {
                console.warn(`AssetLoader: Завершено с ${this.errors.length} ошибками: ${this.errors.join(', ')}`);
            }

            if (this.onComplete) {
                this.onComplete(this.assets);
            }
        }
    }

    // --- Класс игрока ---
    class Player {
        constructor(image, x, y) {
            this.image = image;
            this.baseWidth = image ? image.naturalWidth : 80;
            this.baseHeight = image ? image.naturalHeight : 40;
            this.width = this.baseWidth * SHARK_SCALE;
            this.height = this.baseHeight * SHARK_SCALE;
            this.x = x;
            this.y = y;
            this.angle = 0;
            this.targetAngle = 0;
            this.facingRight = true;
            this.lerpFactor = SHARK_LERP_FACTOR;
            this.hitBox = { width: this.width * 0.7, height: this.height * 0.6 };
            this.pulseEffect = 0;  // Для визуального эффекта щита/иммунитета
            this.isImmune = false;
        }

        update(targetX, targetY, deltaTime) {
            // Движение
            let dx = targetX - this.x;
            let dy = targetY - this.y;

            this.x += dx * this.lerpFactor;
            this.y += dy * this.lerpFactor;

            // Направление взгляда
            if (Math.abs(dx) > 1) {
                this.facingRight = dx > 0;
            }

            // Наклон
            this.targetAngle = Math.max(-SHARK_MAX_TILT, Math.min(SHARK_MAX_TILT, dy * 0.015));
            this.angle += (this.targetAngle - this.angle) * SHARK_TILT_SPEED;

            // Границы игрового поля
            this.x = Math.max(this.width / 2, Math.min(BASE_WIDTH - this.width / 2, this.x));
            this.y = Math.max(this.height / 2, Math.min(BASE_HEIGHT - this.height / 2, this.y));

            // Обновление эффекта пульсации для иммунитета/щита
            if (this.isImmune || activePowerUps.shield.active) {
                this.pulseEffect = (this.pulseEffect + deltaTime * 5) % (Math.PI * 2);
            } else {
                this.pulseEffect = 0;
            }
        }

        draw(ctx) {
            if (!this.image) return;

            ctx.save();

            // Рисуем эффект щита/иммунитета если активен
            if (this.isImmune || activePowerUps.shield.active) {
                const glowColor = activePowerUps.shield.active ? 'rgba(52, 152, 219, 0.5)' : 'rgba(255, 255, 255, 0.4)';
                const glowSize = 1.3 + Math.sin(this.pulseEffect) * 0.1;

                ctx.save();
                ctx.translate(this.x, this.y);
                ctx.scale(glowSize, glowSize);

                // Создаем круговое свечение
                const gradient = ctx.createRadialGradient(0, 0, this.width * 0.3, 0, 0, this.width * 0.7);
                gradient.addColorStop(0, glowColor);
                gradient.addColorStop(1, 'rgba(255, 255, 255, 0)');

                ctx.globalAlpha = 0.7 + Math.sin(this.pulseEffect) * 0.3;
                ctx.fillStyle = gradient;
                ctx.beginPath();
                ctx.arc(0, 0, this.width * 0.7, 0, Math.PI * 2);
                ctx.fill();
                ctx.restore();
            }

            // Рисуем акулу
            ctx.translate(this.x, this.y);
            ctx.rotate(this.angle);
            if (!this.facingRight) ctx.scale(-1, 1);

            // Рисуем эффект магнита если активен
            if (activePowerUps.magnet.active) {
                ctx.save();
                ctx.globalAlpha = 0.3;
                ctx.fillStyle = 'rgba(241, 196, 15, 0.2)';
                ctx.beginPath();
                ctx.arc(0, 0, MAGNET_RADIUS, 0, Math.PI * 2);
                ctx.fill();

                // Контур радиуса действия магнита
                ctx.globalAlpha = 0.7;
                ctx.strokeStyle = 'rgba(241, 196, 15, 0.6)';
                ctx.lineWidth = 3;
                ctx.beginPath();
                ctx.arc(0, 0, MAGNET_RADIUS, 0, Math.PI * 2);
                ctx.stroke();
                ctx.restore();
            }

            // Рисуем эффект звезды если активен
            if (activePowerUps.star.active) {
                ctx.save();
                ctx.globalAlpha = 0.8;

                // Создаем эффект искрения вокруг акулы
                for (let i = 0; i < 6; i++) {
                    const angle = (Math.PI * 2 / 6) * i + (gameTime * 0.001);
                    const x = Math.cos(angle) * (this.width * 0.7);
                    const y = Math.sin(angle) * (this.height * 0.7);
                    const size = 4 + Math.sin(gameTime * 0.01 + i) * 2;

                    ctx.fillStyle = 'rgba(255, 215, 0, 0.8)';
                    ctx.beginPath();
                    ctx.arc(x, y, size, 0, Math.PI * 2);
                    ctx.fill();
                }
                ctx.restore();
            }

            // Рисуем акулу
            ctx.drawImage(this.image, -this.width / 2, -this.height / 2, this.width, this.height);

            ctx.restore();
        }

        collidesWith(item) {
            if (this.isImmune || activePowerUps.shield.active && item.type === 'obstacle') {
                // Нет столкновения, если игрок имеет иммунитет или щит против препятствий
                return false;
            }

            // Проверка эффекта магнита
            if (activePowerUps.magnet.active && item.type === 'treasure') {
                const dx = this.x - item.x - item.width / 2;
                const dy = this.y - item.y - item.height / 2;
                const distance = Math.sqrt(dx * dx + dy * dy);

                if (distance < MAGNET_RADIUS) {
                    // Притягиваем сокровище к игроку
                    const nx = dx / distance;
                    const ny = dy / distance;
                    const attractionForce = (MAGNET_RADIUS - distance) * 0.05;

                    item.x += nx * attractionForce;
                    item.y += ny * attractionForce;
                }
            }

            // Определение столкновения по прямоугольникам
            const sharkLeft = this.x - this.hitBox.width / 2;
            const sharkRight = this.x + this.hitBox.width / 2;
            const sharkTop = this.y - this.hitBox.height / 2;
            const sharkBottom = this.y + this.hitBox.height / 2;

            const itemLeft = item.x;
            const itemRight = item.x + item.width;
            const itemTop = item.y;
            const itemBottom = item.y + item.height;

            // Проверяем, пересекаются ли прямоугольники
            return !(
                sharkRight < itemLeft ||
                sharkLeft > itemRight ||
                sharkBottom < itemTop ||
                sharkTop > itemBottom
            );
        }
    }

    // --- Класс предметов ---
    class Item {
        constructor(image, type, value = 0, expires = null, powerUpType = null) {
            this.image = image;
            this.baseWidth = image ? image.naturalWidth : 30;
            this.baseHeight = image ? image.naturalHeight : 30;
            this.type = type;  // 'treasure', 'obstacle', 'special', 'powerup'
            this.value = value;
            this.expires = expires ? Date.now() + expires : null;
            this.powerUpType = powerUpType;  // 'magnet', 'shield', 'star'
            this.active = true;
            this.rotation = 0;
            this.rotationSpeed = (Math.random() - 0.5) * 0.05;
            this.bobOffset = Math.random() * Math.PI * 2;  // Для анимации вертикального покачивания
            this.bobSpeed = 0.05 + Math.random() * 0.03;

            // Размер в зависимости от типа
            let sizeMultiplier = 1;
            if (type === 'special') sizeMultiplier = 1.5;
            if (type === 'powerup') sizeMultiplier = 1.2;

            // Рассчитываем масштабированный размер с сохранением пропорций
            const ratio = Math.min(
                (ITEM_SIZE * sizeMultiplier) / this.baseWidth,
                (ITEM_SIZE * sizeMultiplier) / this.baseHeight
            );

            this.width = this.baseWidth * ratio;
            this.height = this.baseHeight * ratio;

            // Устанавливаем начальную позицию за правым краем экрана
            this.x = BASE_WIDTH + this.width;
            this.y = Math.random() * (BASE_HEIGHT - this.height * 2) + this.height;

            // Вертикальное покачивание
            this.initialY = this.y;
            this.bobAmplitude = 5 + Math.random() * 5;
        }

        update(speed, deltaTime) {
            // Движение справа налево
            this.x -= speed;

            // Обновление вращения
            this.rotation += this.rotationSpeed;

            // Обновление анимации покачивания
            this.bobOffset += this.bobSpeed;
            this.y = this.initialY + Math.sin(this.bobOffset) * this.bobAmplitude;

            // Проверка, должен ли предмет быть деактивирован
            if (this.x + this.width < 0 || (this.expires && Date.now() > this.expires)) {
                this.active = false;
            }
        }

        draw(ctx) {
            if (!this.image || !this.active) return;

            ctx.save();

            // Устанавливаем центр для вращения
            ctx.translate(this.x + this.width/2, this.y + this.height/2);
            ctx.rotate(this.rotation);

            // Рисуем специальные эффекты в зависимости от типа предмета
            if (this.type === 'powerup') {
                // Свечение для бонусов
                const colors = {
                    'magnet': '#f1c40f',  // Желтый
                    'shield': '#3498db',  // Синий
                    'star': '#e74c3c'     // Красный
                };

                const color = colors[this.powerUpType] || '#ffffff';

                // Рисуем пульсирующее свечение
                const pulseSize = 1 + Math.sin(gameTime * 0.01) * 0.1;
                ctx.globalAlpha = 0.5 + Math.sin(gameTime * 0.01) * 0.2;

                const gradient = ctx.createRadialGradient(0, 0, 0, 0, 0, this.width * 0.8 * pulseSize);
                gradient.addColorStop(0, color);
                gradient.addColorStop(1, 'rgba(255, 255, 255, 0)');

                ctx.fillStyle = gradient;
                ctx.beginPath();
                ctx.arc(0, 0, this.width * 0.8 * pulseSize, 0, Math.PI * 2);
                ctx.fill();

                ctx.globalAlpha = 1.0;
            } else if (this.type === 'special') {
                // Искрящийся эффект для особых предметов
                ctx.globalAlpha = 0.6;
                for (let i = 0; i < 3; i++) {
                    const angle = (Math.PI * 2 / 3) * i + (gameTime * 0.002);
                    const distance = this.width * 0.6;
                    const x = Math.cos(angle) * distance;
                    const y = Math.sin(angle) * distance;
                    const sparkSize = 3 + Math.sin(gameTime * 0.01 + i) * 2;

                    ctx.fillStyle = '#ffffff';
                    ctx.beginPath();
                    ctx.arc(x, y, sparkSize, 0, Math.PI * 2);
                    ctx.fill();
                }
                ctx.globalAlpha = 1.0;
            }

            // Рисуем изображение предмета
            ctx.drawImage(
                this.image,
                -this.width/2,
                -this.height/2,
                this.width,
                this.height
            );

            ctx.restore();

            // Рисуем полоску таймера для исчезающих предметов
            if (this.type === 'special' && this.expires) {
                const timeLeft = Math.max(0, this.expires - Date.now());
                const ratio = timeLeft / 5000;  // 5 секунд - стандартная длительность

                if (ratio > 0) {
                    // Выбираем цвет в зависимости от оставшегося времени
                    ctx.fillStyle = ratio > 0.5 ?
                        'rgba(46, 204, 113, 0.8)' :
                        ratio > 0.2 ? 'rgba(241, 196, 15, 0.8)' : 'rgba(231, 76, 60, 0.8)';

                    // Рисуем полосу таймера
                    const barHeight = 4;
                    const barWidth = this.width;
                    const barX = this.x;
                    const barY = this.y - 10;

                    ctx.fillRect(barX, barY, barWidth * ratio, barHeight);

                    // Рисуем контур
                    ctx.strokeStyle = 'rgba(255, 255, 255, 0.5)';
                    ctx.lineWidth = 1;
                    ctx.strokeRect(barX, barY, barWidth, barHeight);
                }
            }
        }
    }

    // --- Класс частиц ---
    class Particle {
        constructor(x, y, color = '#FFFFFF', size = 3, speed = 2, lifespan = 1.0) {
            this.x = x;
            this.y = y;
            this.baseSize = size;
            this.speed = speed;
            this.lifespan = lifespan * 1000;  // Переводим в миллисекунды
            this.spawnTime = Date.now();

            // Случайная скорость
            const angle = Math.random() * Math.PI * 2;
            const velocity = Math.random() * this.speed + 0.5;
            this.vx = Math.cos(angle) * velocity;
            this.vy = Math.sin(angle) * velocity;

            // Парсим цвет
            const rgb = this.hexToRgb(color) || { r: 255, g: 255, b: 255 };
            this.r = rgb.r;
            this.g = rgb.g;
            this.b = rgb.b;

            this.alpha = 1;
            this.active = true;
        }

        update(deltaTime) {
            // Обновляем позицию
            this.x += this.vx;
            this.y += this.vy;

            // Применяем гравитацию
            this.vy += 0.02;

            // Рассчитываем возраст и прозрачность
            const age = (Date.now() - this.spawnTime) / this.lifespan;
            this.alpha = 1 - age;

            // Деактивируем слишком старые частицы
            if (this.alpha <= 0) {
                this.active = false;
            }
        }

        draw(ctx) {
            if (!this.active || this.alpha <= 0) return;

            // Рассчитываем текущий размер на основе жизненного цикла
            const currentSize = this.baseSize * this.alpha;
            if (currentSize < 0.5) return;

            // Рисуем частицу
            ctx.fillStyle = `rgba(${this.r}, ${this.g}, ${this.b}, ${this.alpha})`;
            ctx.beginPath();
            ctx.arc(this.x, this.y, currentSize, 0, Math.PI * 2);
            ctx.fill();
        }

        hexToRgb(hex) {
            // Конвертируем hex цвет в RGB
            let r = 0, g = 0, b = 0;

            if (hex.length === 4) {
                r = parseInt(hex[1] + hex[1], 16);
                g = parseInt(hex[2] + hex[2], 16);
                b = parseInt(hex[3] + hex[3], 16);
            } else if (hex.length === 7) {
                r = parseInt(hex[1] + hex[2], 16);
                g = parseInt(hex[3] + hex[4], 16);
                b = parseInt(hex[5] + hex[6], 16);
            }

            if (isNaN(r) || isNaN(g) || isNaN(b)) return null;

            return { r, g, b };
        }
    }

    // --- Класс пузырьков ---
    class Bubble {
        constructor() {
            // Случайный размер и позиция
            this.size = MIN_BUBBLE_SIZE + Math.random() * (MAX_BUBBLE_SIZE - MIN_BUBBLE_SIZE);
            this.x = Math.random() * BASE_WIDTH;
            this.y = BASE_HEIGHT + this.size;

            // Случайная скорость и горизонтальный дрейф
            this.speed = MIN_BUBBLE_SPEED + Math.random() * (MAX_BUBBLE_SPEED - MIN_BUBBLE_SPEED);
            this.horizontalDrift = (Math.random() - 0.5) * 0.5;

            // Внешний вид
            this.opacity = 0.1 + Math.random() * 0.3;
        }

        update(deltaTime) {
            this.y -= this.speed;
            this.x += this.horizontalDrift;

            // Сбрасываем пузырек, если он выходит за пределы экрана
            if (this.y < -this.size) {
                this.reset();
            }
        }

        draw(ctx) {
            ctx.save();
            ctx.globalAlpha = this.opacity;
            ctx.fillStyle = 'rgba(255, 255, 255, 0.5)';
            ctx.strokeStyle = 'rgba(255, 255, 255, 0.8)';
            ctx.lineWidth = 1;

            ctx.beginPath();
            ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
            ctx.fill();
            ctx.stroke();

            // Добавляем блик на пузырек
            ctx.beginPath();
            ctx.arc(this.x - this.size * 0.3, this.y - this.size * 0.3, this.size * 0.2, 0, Math.PI * 2);
            ctx.fillStyle = 'rgba(255, 255, 255, 0.8)';
            ctx.fill();

            ctx.restore();
        }

        reset() {
            this.size = MIN_BUBBLE_SIZE + Math.random() * (MAX_BUBBLE_SIZE - MIN_BUBBLE_SIZE);
            this.x = Math.random() * BASE_WIDTH;
            this.y = BASE_HEIGHT + this.size;
            this.speed = MIN_BUBBLE_SPEED + Math.random() * (MAX_BUBBLE_SPEED - MIN_BUBBLE_SPEED);
            this.horizontalDrift = (Math.random() - 0.5) * 0.5;
            this.opacity = 0.1 + Math.random() * 0.3;
        }
    }

    // --- Основные функции игры ---

    function resizeCanvas() {
        // Получаем размеры контейнера
        const containerWidth = gameArea.clientWidth;
        const containerHeight = gameArea.clientHeight;

        if (containerWidth === 0 || containerHeight === 0) {
            console.warn("Контейнер имеет нулевые размеры. Пропускаем изменение размера.");
            return;
        }

        // Для широкой адаптивной игры устанавливаем физические размеры холста = размерам контейнера
        currentCanvasWidth = containerWidth;
        currentCanvasHeight = containerHeight;

        // Устанавливаем размеры холста в соответствии с размерами контейнера
        canvas.width = containerWidth;
        canvas.height = containerHeight;

        // Рассчитываем масштаб для преобразования логических координат в физические
        scale = Math.min(containerWidth / BASE_WIDTH, containerHeight / BASE_HEIGHT);

        console.log(`Холст изменен: ${currentCanvasWidth.toFixed(0)}x${currentCanvasHeight.toFixed(0)}, Масштаб=${scale.toFixed(2)}`);

        // Отрисовываем кадр после изменения размера
        if (gameState === 'PLAYING' || gameState === 'GAME_OVER' || gameState === 'READY') {
            renderGame();
        }
    }

    function getLogicalMousePos(event) {
        // Конвертируем экранные координаты в логические игровые координаты
        const rect = canvas.getBoundingClientRect();
        const clientX = event.clientX - rect.left;
        const clientY = event.clientY - rect.top;

        // Преобразуем в логические координаты в соответствии с масштабом холста
        return {
            x: Math.max(0, Math.min(BASE_WIDTH, clientX / scale)),
            y: Math.max(0, Math.min(BASE_HEIGHT, clientY / scale))
        };
    }

    function loadAssets() {
        console.log("Начинаем загрузку ресурсов...");
        gameState = 'LOADING';

        // Обновляем интерфейс загрузки
        if (loadingScreen) loadingScreen.style.display = 'flex';
        if (loadingBar) loadingBar.style.width = '0%';
        if (loadingText) loadingText.textContent = 'Загрузка ресурсов...';

        // Определяем ресурсы для загрузки
        const assetMap = {
            shark: 'img-shark',
            coin: 'img-coin',
            pearl: 'img-pearl',
            shell: 'img-shell',
            pufferfish: 'img-pufferfish',
            urchin: 'img-urchin',
            trash: 'img-trash',
            chest: 'img-chest',
            background1: 'img-background1',
            background2: 'img-background2',
            background3: 'img-background3',
            background4: 'img-background4',
            magnet: 'img-magnet',
            shield: 'img-shield',
            star: 'img-star'
        };

        totalAssets = Object.keys(assetMap).length;

        // Создаем и запускаем загрузчик ресурсов
        const loader = new AssetLoader(
            assetMap,
            onAssetsLoaded,
            onAssetProgress
        );

        loader.load();
    }

    function onAssetProgress(loaded, total, progress) {
        // Обновляем полосу загрузки
        if (loadingBar) loadingBar.style.width = `${progress}%`;
        if (loadingText) loadingText.textContent = `Загрузка ресурсов... ${loaded}/${total}`;
    }

    function onAssetsLoaded(loadedAssets) {
        assets = loadedAssets;
        console.log("Ресурсы загружены:", Object.keys(assets));

        // Проверка критических ресурсов
        if (!assets.shark) {
            console.error("Критические ресурсы не загружены!");
            setGameOverState("Ошибка загрузки!<br>Не удалось загрузить необходимые ресурсы.");
            return;
        }

        // Создаем игрока
        shark = new Player(assets.shark, BASE_WIDTH / 2, BASE_HEIGHT / 2);

        // Создаем пузырьки
        createBubbles();

        // Инициализируем игру
        gameState = 'READY';
        prevTime = performance.now();

        // Скрываем экран загрузки с задержкой
        setTimeout(() => {
            if (loadingScreen) loadingScreen.style.display = 'none';

            // Показываем стартовое сообщение
            gameMessage.innerHTML = `
                <div style="color: #3498db; font-size: 32px; margin-bottom: 20px;">Deeray Shark Hunter</div>
                <div style="color: #ecf0f1; font-size: 18px; margin-bottom: 15px;">
                    Собирайте сокровища и избегайте препятствий.
                    <br>Повышайте уровень для новых испытаний!
                </div>
                <small>Нажмите, чтобы начать...</small>
            `;
            gameMessageContainer.style.display = 'flex';

            // ВАЖНОЕ ИСПРАВЛЕНИЕ: Устанавливаем обработчик клика напрямую, а не через onclick
            const startGameHandler = function() {
                startGame();
                gameMessageContainer.removeEventListener('click', startGameHandler);
            };

            gameMessageContainer.addEventListener('click', startGameHandler);

            // Запускаем игровой цикл для отрисовки стартового экрана
            if (!animationFrameId) {
                animationFrameId = requestAnimationFrame(gameLoop);
            }
        }, 500);
    }

    function createBubbles() {
        bubbles = [];
        for (let i = 0; i < BUBBLE_COUNT; i++) {
            bubbles.push(new Bubble());
        }
    }

    function startGame() {
        console.log("Запуск игры");

        // Скрываем сообщение
        gameMessageContainer.style.display = 'none';

        // Сбрасываем игровые переменные
        score = 0;
        combo = 1;
        comboTimer = 0;
        level = 1;
        lives = 3;
        gameTime = 0;
        backgroundChangeTimer = 0;
        itemSpeed = INITIAL_ITEM_SPEED;
        spawnInterval = INITIAL_SPAWN_INTERVAL;
        obstacleProbability = INITIAL_OBSTACLE_PROBABILITY;
        spawnTimer = 0;
        items = [];
        particles = [];
        isImmune = false;
        immuneTimer = 0;

        // Сбрасываем бонусы
        activePowerUps = {
            magnet: { active: false, endTime: 0 },
            shield: { active: false, endTime: 0 },
            star: { active: false, endTime: 0 }
        };

        // Сбрасываем статистику
        gameStats = {
            treasuresCollected: 0,
            obstaclesHit: 0,
            powerUpsCollected: 0,
            maxCombo: 1,
            timeAlive: 0
        };

        // Обновляем интерфейс
        updateUI();

        // Если акула уже создана, сбрасываем её позицию
        if (shark) {
            shark.x = BASE_WIDTH / 2;
            shark.y = BASE_HEIGHT / 2;
            shark.angle = 0;
            shark.facingRight = true;
            shark.isImmune = false;
        }

        // Устанавливаем состояние игры
        gameState = 'PLAYING';

        // Очищаем индикатор бонусов
        if (powerUpIndicator) {
            powerUpIndicator.innerHTML = '';
        }

        // Скрываем курсор
        canvas.style.cursor = 'none';

        // Запускаем игровой цикл, если он еще не запущен
        if (!animationFrameId) {
            animationFrameId = requestAnimationFrame(gameLoop);
        }
    }

    function gameLoop(timestamp) {
        // Рассчитываем дельту времени
        const deltaTime = Math.min(50, timestamp - prevTime) / 1000; // Ограничиваем до 50мс
        prevTime = timestamp;

        // Обновляем состояние игры
        if (gameState === 'PLAYING') {
            updateGame(deltaTime);
            gameTime += deltaTime * 1000;

            // Таймер для автоматической смены фона
            backgroundChangeTimer += deltaTime * 1000;
            if (backgroundChangeTimer >= BACKGROUND_CHANGE_INTERVAL) {
                changeBackground();
                backgroundChangeTimer = 0;
            }
        }

        // Всегда обновляем анимацию перехода фона
        updateBackgroundTransition(deltaTime);

        // Всегда рендерим (даже во время паузы/окончания игры)
        renderGame(deltaTime);

        // Продолжаем цикл, если игра активна
        if (gameState !== 'GAME_OVER') {
            animationFrameId = requestAnimationFrame(gameLoop);
        } else {
            console.log("Игровой цикл остановлен");
        }
    }

    function updateGame(deltaTime) {
        // Обновляем позицию игрока
        if (shark) {
            shark.update(logicalMousePos.x, logicalMousePos.y, deltaTime);
        }

        // Обновляем статус иммунитета
        if (isImmune) {
            immuneTimer -= deltaTime * 1000;
            if (immuneTimer <= 0) {
                isImmune = false;
                if (shark) shark.isImmune = false;
            }
        }

        // Обновляем таймеры способностей
        updatePowerUps(deltaTime);

        // Обновляем все игровые объекты
        updateItems(deltaTime);
        updateParticles(deltaTime);
        updateBubbles(deltaTime);

        // Проверяем таймаут комбо
        if (combo > 1) {
            comboTimer += deltaTime * 1000;
            if (comboTimer >= COMBO_TIMEOUT) {
                resetCombo();
            }
        }

        // Спавним новые предметы
        spawnTimer += deltaTime * 1000;
        if (spawnTimer >= spawnInterval) {
            spawnItem();
            spawnTimer = 0;
        }

        // Проверяем столкновения
        checkCollisions();

        // Обновляем интерфейс
        updateUI();
    }

    function updatePowerUps(deltaTime) {
        const currentTime = Date.now();
        let powerupChanged = false;

        // Проверяем все способности
        for (const [type, data] of Object.entries(activePowerUps)) {
            if (data.active && currentTime > data.endTime) {
                data.active = false;
                powerupChanged = true;
                console.log(`Бонус ${type} истек`);
            }
        }

        // Обновляем индикатор бонусов, если изменилось состояние
        if (powerupChanged) {
            updatePowerUpIndicator();
        }
    }

    function updatePowerUpIndicator() {
        // Очищаем существующие индикаторы
        if (!powerUpIndicator) return;
        powerUpIndicator.innerHTML = '';

        // Создаем индикаторы для активных бонусов
        for (const [type, data] of Object.entries(activePowerUps)) {
            if (data.active) {
                const timeLeft = Math.max(0, data.endTime - Date.now());
                const percentage = timeLeft / POWERUP_DURATION * 100;

                // Создаем индикатор бонуса
                const indicator = document.createElement('div');
                indicator.className = 'powerup-icon active';

                // Добавляем иконку
                const img = document.createElement('img');
                img.src = assets[type].src;
                indicator.appendChild(img);

                // Добавляем таймер
                const timerContainer = document.createElement('div');
                timerContainer.className = 'powerup-timer';

                const timerBar = document.createElement('div');
                timerBar.className = 'powerup-timer-bar';
                timerBar.style.width = `${percentage}%`;

                timerContainer.appendChild(timerBar);
                indicator.appendChild(timerContainer);

                powerUpIndicator.appendChild(indicator);
            }
        }
    }

    function updateItems(deltaTime) {
        // Обновляем и фильтруем неактивные предметы
        items = items.filter(item => {
            item.update(itemSpeed, deltaTime);
            return item.active;
        });
    }

    function updateParticles(deltaTime) {
        // Обновляем и фильтруем неактивные частицы
        particles = particles.filter(particle => {
            particle.update(deltaTime);
            return particle.active;
        });
    }

    function updateBubbles(deltaTime) {
        // Обновляем все пузырьки
        bubbles.forEach(bubble => bubble.update(deltaTime));
    }

    function updateBackgroundTransition(deltaTime) {
        // Обновляем прогресс перехода фона
        if (backgroundTransition.active) {
            backgroundTransition.progress += deltaTime / (backgroundTransition.duration / 1000);

            if (backgroundTransition.progress >= 1) {
                backgroundTransition.active = false;
                backgroundTransition.progress = 0;
            }
        }
    }

    function renderGame(deltaTime) {
        // Сохраняем состояние контекста
        ctx.save();

        // Очищаем холст (размером с контейнер)
        ctx.clearRect(0, 0, canvas.width, canvas.height);

        // Устанавливаем масштаб для логических координат
        ctx.scale(scale, scale);

        // Рисуем фон
        drawBackground();

        // Рисуем пузырьки
        bubbles.forEach(bubble => bubble.draw(ctx));

        // Рисуем все игровые объекты
        items.forEach(item => item.draw(ctx));

        // Рисуем игрока
        if (shark && gameState !== 'GAME_OVER') {
            shark.draw(ctx);
        }

        // Рисуем частицы поверх всего
        particles.forEach(particle => particle.draw(ctx));

        // Восстанавливаем контекст
        ctx.restore();
    }

    function drawBackground() {
        // Рисуем фон с плавным переходом между изображениями
        if (backgroundTransition.active) {
            // Отрисовка с прозрачностью для эффекта перехода
            ctx.globalAlpha = 1 - backgroundTransition.progress;

            // Рисуем уходящий фон
            const fromImg = assets[`background${backgroundTransition.from + 1}`];
            if (fromImg) {
                ctx.drawImage(fromImg, 0, 0, BASE_WIDTH, BASE_HEIGHT);
            }

            // Рисуем новый фон с нарастающей прозрачностью
            ctx.globalAlpha = backgroundTransition.progress;
            const toImg = assets[`background${backgroundTransition.to + 1}`];
            if (toImg) {
                ctx.drawImage(toImg, 0, 0, BASE_WIDTH, BASE_HEIGHT);
            }

            // Восстанавливаем прозрачность
            ctx.globalAlpha = 1.0;
        } else {
            // Рисуем текущий фон без эффекта перехода
            const backgroundImg = assets[`background${backgroundIndex + 1}`];
            if (backgroundImg) {
                ctx.drawImage(backgroundImg, 0, 0, BASE_WIDTH, BASE_HEIGHT);
            } else {
                // Запасной вариант, если изображение не загружено
                const gradient = ctx.createLinearGradient(0, 0, 0, BASE_HEIGHT);
                gradient.addColorStop(0, '#0a1622');
                gradient.addColorStop(1, '#1a3c5f');
                ctx.fillStyle = gradient;
                ctx.fillRect(0, 0, BASE_WIDTH, BASE_HEIGHT);
            }
        }
    }

    function changeBackground() {
        if (!backgroundTransition.active) {
            backgroundTransition.active = true;
            backgroundTransition.from = backgroundIndex;
            backgroundTransition.to = (backgroundIndex + 1) % 4;
            backgroundTransition.progress = 0;

            // Устанавливаем новый индекс фона
            backgroundIndex = (backgroundIndex + 1) % 4;
        }
    }

    function spawnItem() {
        // Определяем тип предмета на основе вероятностей
        const rand = Math.random();
        let type = 'treasure';
        let imageKey = 'coin';
        let value = 10;
        let expires = null;
        let powerUpType = null;

        // Логика спавна бонусов
        if (rand < POWERUP_PROBABILITY && (assets.magnet || assets.shield || assets.star)) {
            type = 'powerup';

            // Выбираем тип бонуса
            const powerUpRand = Math.random();
            if (powerUpRand < 0.33 && assets.magnet) {
                powerUpType = 'magnet';
                imageKey = 'magnet';
            } else if (powerUpRand < 0.66 && assets.shield) {
                powerUpType = 'shield';
                imageKey = 'shield';
            } else if (assets.star) {
                powerUpType = 'star';
                imageKey = 'star';
            } else if (assets.magnet) {
                powerUpType = 'magnet';
                imageKey = 'magnet';
            } else if (assets.shield) {
                powerUpType = 'shield';
                imageKey = 'shield';
            } else {
                // Если нет изображений бонусов, меняем тип на сокровище
                type = 'treasure';
                imageKey = 'coin';
                powerUpType = null;
            }

            value = 50;
        }
        // Особый предмет (сундук с сокровищами)
        else if (rand < POWERUP_PROBABILITY + SPECIAL_PROBABILITY && assets.chest) {
            type = 'special';
            imageKey = 'chest';
            value = 100 * level;  // Масштабируем с уровнем
            expires = 5000;  // 5 секунд на сбор
        }
        // Препятствие
        else if (rand < POWERUP_PROBABILITY + SPECIAL_PROBABILITY + obstacleProbability) {
            type = 'obstacle';
            value = 0;

            // Выбираем тип препятствия
            const obstacleRand = Math.random();
            if (obstacleRand < 0.33 && assets.pufferfish) {
                imageKey = 'pufferfish';
            } else if (obstacleRand < 0.66 && assets.urchin) {
                imageKey = 'urchin';
            } else if (assets.trash) {
                imageKey = 'trash';
            } else if (assets.pufferfish) {
                imageKey = 'pufferfish';
            } else {
                // Запасной вариант для препятствий
                imageKey = assets.trash ? 'trash' : null;
            }
        }
        // Обычное сокровище
        else {
            type = 'treasure';

            // Выбираем тип сокровища
            const treasureRand = Math.random();
            if (treasureRand < 0.5 && assets.coin) {
                imageKey = 'coin';
                value = 10;
            } else if (treasureRand < 0.8 && assets.shell) {
                imageKey = 'shell';
                value = 15;
            } else if (assets.pearl) {
                imageKey = 'pearl';
                value = 25;
            } else {
                // Запасной вариант
                imageKey = assets.coin ? 'coin' : null;
            }

            // Масштабируем ценность с уровнем
            value = Math.floor(value * (1 + (level - 1) * 0.2));
        }

        // Создаем предмет, если есть изображение
        if (imageKey && assets[imageKey]) {
            items.push(new Item(assets[imageKey], type, value, expires, powerUpType));
        }
    }

    function checkCollisions() {
        if (!shark) return;

        // Проверяем каждый предмет на столкновение с игроком
        items = items.filter(item => {
            if (shark.collidesWith(item)) {
                handleCollision(item);
                return false;  // Удаляем предмет из массива
            }
            return true;  // Оставляем предмет
        });
    }

    function handleCollision(item) {
        if (item.type === 'treasure' || item.type === 'special') {
            // Обработка сбора сокровищ
            const comboMultiplier = Math.min(combo, COMBO_MULTIPLIER_MAX);
            const pointValue = item.value * comboMultiplier;

            // Двойные очки с бонусом звезды
            const finalScore = activePowerUps.star.active ? pointValue * 2 : pointValue;

            // Обновляем очки
            score += finalScore;
            gameStats.treasuresCollected++;

            // Обновляем комбо
            combo++;
            comboTimer = 0;

            // Обновляем статистику максимального комбо
            if (combo > gameStats.maxCombo) {
                gameStats.maxCombo = combo;
            }

            // Создаем визуальный эффект
            const particleColor = item.type === 'special' ? '#FFD700' : '#FFFFFF';
            createParticles(
                item.x + item.width / 2,
                item.y + item.height / 2,
                particleColor,
                PARTICLE_COUNT_COLLECT
            );

            // Проверяем повышение уровня
            checkLevelUp();

        } else if (item.type === 'obstacle') {
            // Обработка столкновения с препятствием
            if (!isImmune && !activePowerUps.shield.active) {
                // Получаем урон
                lives--;
                gameStats.obstaclesHit++;

                // Создаем эффект урона
                createParticles(shark.x, shark.y, '#FF0000', PARTICLE_COUNT_HIT);

                // Временный иммунитет
                isImmune = true;
                immuneTimer = IMMUNITY_DURATION;
                if (shark) shark.isImmune = true;

                // Сбрасываем комбо
                resetCombo();

                // Проверяем окончание игры
                if (lives <= 0) {
                    setGameOverState();
                }
            } else {
                // Создаем эффект блокирования щитом
                createParticles(
                    item.x + item.width / 2,
                    item.y + item.height / 2,
                    '#3498db',  // Синий эффект щита
                    15
                );
            }
        } else if (item.type === 'powerup') {
            // Обработка сбора бонуса
            activatePowerUp(item.powerUpType);
            gameStats.powerUpsCollected++;

            // Создаем эффект сбора бонуса
            const powerUpColors = {
                'magnet': '#f1c40f',  // Желтый
                'shield': '#3498db',  // Синий
                'star': '#e74c3c'     // Красный
            };

            createParticles(
                item.x + item.width / 2,
                item.y + item.height / 2,
                powerUpColors[item.powerUpType] || '#FFFFFF',
                PARTICLE_COUNT_COLLECT * 2
            );
        }
    }

    function activatePowerUp(type) {
        if (!type || !activePowerUps[type]) return;

        // Активируем бонус
        activePowerUps[type].active = true;
        activePowerUps[type].endTime = Date.now() + POWERUP_DURATION;

        console.log(`Бонус ${type} активирован на ${POWERUP_DURATION/1000} секунд`);

        // Обновляем индикатор бонусов
        updatePowerUpIndicator();
    }

    function checkLevelUp() {
        // Проверяем, нужно ли повысить уровень
        const nextLevel = Math.floor(score / POINTS_PER_LEVEL) + 1;

        if (nextLevel > level && nextLevel <= MAX_LEVEL) {
            // Повышаем уровень
            level = nextLevel;

            // Увеличиваем сложность
            itemSpeed = INITIAL_ITEM_SPEED + (level - 1) * 0.4;
            spawnInterval = INITIAL_SPAWN_INTERVAL - (level - 1) * 50;
            obstacleProbability = INITIAL_OBSTACLE_PROBABILITY + (level - 1) * 0.03;

            // Обеспечиваем минимальный интервал спавна
            spawnInterval = Math.max(spawnInterval, 300);

            // Эффект повышения уровня
            if (levelUpAnimation) {
                levelUpAnimation.classList.remove('level-up-active');
                void levelUpAnimation.offsetWidth; // Вызываем reflow
                levelUpAnimation.classList.add('level-up-active');
            }

            // Меняем фон при повышении уровня
            changeBackground();
            backgroundChangeTimer = 0;

            console.log(`Уровень повышен! Теперь уровень ${level}`);
        }
    }

    function createParticles(x, y, color = '#FFFFFF', count = 10) {
        // Создаем несколько частиц в указанной позиции
        for (let i = 0; i < count; i++) {
            particles.push(
                new Particle(
                    x,
                    y,
                    color,
                    Math.random() * 4 + 2,  // Размер
                    PARTICLE_SPEED,
                    PARTICLE_LIFESPAN
                )
            );
        }
    }

    function resetCombo() {
        if (combo > 1) {
            combo = 1;
            comboTimer = 0;
            updateUI();
        }
    }

    function setGameOverState() {
        if (gameState === 'GAME_OVER') return;

        // Устанавливаем состояние игры
        gameState = 'GAME_OVER';

        if (animationFrameId) {
        cancelAnimationFrame(animationFrameId);
        animationFrameId = null;
    }

        // Рассчитываем итоговую статистику
        gameStats.timeAlive = Math.floor(gameTime / 1000);  // Переводим в секунды

        // Создаем сообщение об окончании игры со статистикой
        gameMessage.innerHTML = `
            <div style="color: #e74c3c; margin-bottom: 15px;">Игра окончена!</div>
            <div style="color: #ecf0f1; font-size: 24px; margin-bottom: 10px;">Очки: ${score}</div>
            <div class="stats">
                <div>Уровень: <span>${level}</span></div>
                <div>Время игры: <span>${formatTime(gameStats.timeAlive)}</span></div>
                <div>Собрано сокровищ: <span>${gameStats.treasuresCollected}</span></div>
                <div>Собрано бонусов: <span>${gameStats.powerUpsCollected}</span></div>
                <div>Макс. комбо: <span>x${gameStats.maxCombo}</span></div>
            </div>
            <small>Сможете лучше?</small>
        `;

        // Показываем интерфейс окончания игры
        gameMessageContainer.style.display = 'flex';
        restartButton.style.display = 'block';
        canvas.style.cursor = 'default';

        console.log("Игра окончена");
    }

    function formatTime(seconds) {
        const mins = Math.floor(seconds / 60);
        const secs = seconds % 60;
        return `${mins}:${secs.toString().padStart(2, '0')}`;
    }

    function updateUI() {
        // Обновляем отображение очков и комбо
        scoreDisplay.textContent = `Очки: ${score}`;
        comboDisplay.textContent = `Комбо: x${combo}`;

        if (levelDisplay) levelDisplay.textContent = `Уровень: ${level}`;
        if (livesDisplay) livesDisplay.textContent = `Жизни: ${lives}`;

        // Обновляем цвет комбо в зависимости от размера
        if (combo >= 10) {
            comboDisplay.style.color = '#e74c3c';  // Красный для высокого комбо
        } else if (combo >= 5) {
            comboDisplay.style.color = '#e67e22';  // Оранжевый для среднего комбо
        } else {
            comboDisplay.style.color = '#f1c40f';  // Желтый для низкого комбо
        }
    }

    // --- Обработчики событий ---

    function handleMouseMove(e) {
        if (gameState === 'PLAYING') {
            logicalMousePos = getLogicalMousePos(e);
        }
    }

    function handleMouseLeave() {
        if (gameState === 'PLAYING') {
            canvas.style.cursor = 'default';
        }
    }

    function handleMouseEnter() {
        if (gameState === 'PLAYING') {
            canvas.style.cursor = 'none';
        }
    }

    function handleRestartClick() {
        // ВАЖНО: Исправлен обработчик для корректного перезапуска
        if (gameState === 'GAME_OVER') {
            // Скрываем кнопку перезапуска
            restartButton.style.display = 'none';

            // Вызываем startGame напрямую без обработчиков событий
            startGame();
        }
    }

    function handleWindowResize() {
        resizeCanvas();
    }

    // --- Инициализация ---

    // Добавляем обработчики событий
    canvas.addEventListener('mousemove', handleMouseMove);
    canvas.addEventListener('mouseleave', handleMouseLeave);
    canvas.addEventListener('mouseenter', handleMouseEnter);

    // ВАЖНО: Используем addEventListener вместо прямого назначения
    if (restartButton) {
        restartButton.addEventListener('click', handleRestartClick);
    }

    window.addEventListener('resize', handleWindowResize);

    // Инициализируем игру
    resizeCanvas();
    loadAssets();
});