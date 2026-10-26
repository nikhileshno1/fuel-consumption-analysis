-- Fuel Consumption Analysis System: schema + sample data
-- Run:  mysql -u root -p < schema.sql

CREATE DATABASE IF NOT EXISTS fuel_consumption_db;
USE fuel_consumption_db;

CREATE TABLE IF NOT EXISTS vehicles (
    vehicle_id   INT AUTO_INCREMENT PRIMARY KEY,
    vehicle_code VARCHAR(30) NOT NULL UNIQUE,
    vehicle_type VARCHAR(30) NOT NULL,
    fuel_type    VARCHAR(30) NOT NULL
);

-- No CHECK on litres/odometer order on purpose: the app rejects bad input,
-- but the invalid-reading report must be able to surface bad rows that were
-- imported directly into the database (CSV load, manual SQL, older data).
CREATE TABLE IF NOT EXISTS fuel_records (
    record_id      INT AUTO_INCREMENT PRIMARY KEY,
    vehicle_id     INT NOT NULL,
    fill_date      DATE NOT NULL,
    odometer_km    DECIMAL(10,2) NOT NULL,
    litres         DECIMAL(8,3) NOT NULL,
    total_cost     DECIMAL(10,2) NOT NULL,
    full_tank_flag TINYINT(1) NOT NULL DEFAULT 1,
    CONSTRAINT fk_fuel_vehicle FOREIGN KEY (vehicle_id) REFERENCES vehicles(vehicle_id),
    INDEX idx_vehicle_date (vehicle_id, fill_date, record_id)
);

-- ---------------------------------------------------------------------------
-- Sample data (run once; delete rows first if you re-run)
-- ---------------------------------------------------------------------------
INSERT INTO vehicles (vehicle_code, vehicle_type, fuel_type) VALUES
    ('CAR001',  'Car',   'Petrol'),
    ('CAR002',  'Car',   'Diesel'),
    ('BIKE001', 'Bike',  'Petrol'),
    ('TRK001',  'Truck', 'Diesel');

-- CAR001: petrol hatchback, one partial fill (07-08) before a full fill
INSERT INTO fuel_records (vehicle_id, fill_date, odometer_km, litres, total_cost, full_tank_flag)
SELECT vehicle_id, d, o, l, c, f FROM vehicles JOIN (
    SELECT '2026-06-03' d, 12000 o, 32.0 l, 3360.00 c, 1 f UNION ALL
    SELECT '2026-06-20', 12510, 31.5, 3307.50, 1 UNION ALL
    SELECT '2026-07-08', 13015, 20.0, 2100.00, 0 UNION ALL
    SELECT '2026-07-15', 13330, 31.3, 3286.50, 1 UNION ALL
    SELECT '2026-08-02', 13840, 31.7, 3328.50, 1 UNION ALL
    SELECT '2026-08-25', 14370, 33.0, 3465.00, 1 UNION ALL
    SELECT '2026-09-12', 14880, 31.9, 3349.50, 1 UNION ALL
    SELECT '2026-10-05', 15395, 32.3, 3391.50, 1
) x WHERE vehicle_code = 'CAR001';

-- CAR002: diesel sedan; 07-30 has an odometer typo (3005 instead of ~30050)
INSERT INTO fuel_records (vehicle_id, fill_date, odometer_km, litres, total_cost, full_tank_flag)
SELECT vehicle_id, d, o, l, c, f FROM vehicles JOIN (
    SELECT '2026-06-05' d, 28000 o, 40.0 l, 3680.00 c, 1 f UNION ALL
    SELECT '2026-06-24', 28810, 40.1, 3689.20, 1 UNION ALL
    SELECT '2026-07-12', 29600, 39.5, 3634.00, 1 UNION ALL
    SELECT '2026-07-30',  3005, 22.0, 2024.00, 0 UNION ALL
    SELECT '2026-08-18', 30410, 40.5, 3726.00, 1 UNION ALL
    SELECT '2026-09-06', 31225, 40.8, 3753.60, 1 UNION ALL
    SELECT '2026-09-28', 32030, 40.2, 3698.40, 1 UNION ALL
    SELECT '2026-10-12', 32520, 24.5, 2254.00, 0
) x WHERE vehicle_code = 'CAR002';

-- BIKE001: 08-14 has zero litres (bad import)
INSERT INTO fuel_records (vehicle_id, fill_date, odometer_km, litres, total_cost, full_tank_flag)
SELECT vehicle_id, d, o, l, c, f FROM vehicles JOIN (
    SELECT '2026-06-08' d, 5200 o, 9.0 l, 945.00 c, 1 f UNION ALL
    SELECT '2026-06-28', 5600, 8.8, 924.00, 1 UNION ALL
    SELECT '2026-07-18', 5985, 8.5, 892.50, 1 UNION ALL
    SELECT '2026-08-04', 6200, 4.0, 420.00, 0 UNION ALL
    SELECT '2026-08-14', 6390, 0.0, 0.00, 1 UNION ALL
    SELECT '2026-08-30', 6700, 6.9, 724.50, 1 UNION ALL
    SELECT '2026-09-20', 7120, 9.2, 966.00, 1 UNION ALL
    SELECT '2026-10-10', 7545, 9.4, 987.00, 1
) x WHERE vehicle_code = 'BIKE001';

-- TRK001: 08-09 repeats the previous odometer reading
INSERT INTO fuel_records (vehicle_id, fill_date, odometer_km, litres, total_cost, full_tank_flag)
SELECT vehicle_id, d, o, l, c, f FROM vehicles JOIN (
    SELECT '2026-06-10' d, 80000 o, 120.0 l, 11040.00 c, 1 f UNION ALL
    SELECT '2026-06-30', 80600, 120.5, 11086.00, 1 UNION ALL
    SELECT '2026-07-20', 81200, 118.0, 10856.00, 1 UNION ALL
    SELECT '2026-08-09', 81200, 60.0, 5520.00, 0 UNION ALL
    SELECT '2026-08-25', 81790, 116.0, 10672.00, 1 UNION ALL
    SELECT '2026-09-14', 82390, 120.0, 11040.00, 1 UNION ALL
    SELECT '2026-10-04', 82985, 118.5, 10902.00, 1 UNION ALL
    SELECT '2026-10-14', 83300, 62.0, 5704.00, 0
) x WHERE vehicle_code = 'TRK001';