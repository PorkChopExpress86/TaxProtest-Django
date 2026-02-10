def calculate_score_old(target_area, candidate_area):
    diff_pct = abs(target_area - candidate_area) / target_area
    max_land_points = 90.0
    points = 0
    if diff_pct <= 0.1: points = max_land_points
    elif diff_pct <= 0.2: points = max_land_points * 0.8
    elif diff_pct <= 0.35: points = max_land_points * 0.53
    elif diff_pct <= 0.5: points = max_land_points * 0.27
    elif diff_pct <= 1.0: points = max_land_points * 0.25
    return points

def calculate_score_new(target_area, candidate_area):
    diff_pct = abs(target_area - candidate_area) / target_area
    max_land_points = 90.0
    points = 0
    
    # Granular scoring
    if diff_pct <= 0.01: # 1%
        points = max_land_points 
    elif diff_pct <= 0.05: # 5%
        # Linear drop from 90 to 80
        # 0.01 -> 90
        # 0.05 -> 80
        ratio = (diff_pct - 0.01) / 0.04
        points = 90 - (10 * ratio)
    elif diff_pct <= 0.1: # 10%
        # Linear drop from 80 to 70
        ratio = (diff_pct - 0.05) / 0.05
        points = 80 - (10 * ratio)
    elif diff_pct <= 0.2: # 20%
        # Linear drop from 70 to 50
        ratio = (diff_pct - 0.1) / 0.1
        points = 70 - (20 * ratio)
    elif diff_pct <= 0.5:
        # Linear drop from 50 to 20
        ratio = (diff_pct - 0.2) / 0.3
        points = 50 - (30 * ratio)
    else:
        points = 10
        
    return round(points, 1)

target = 10000
candidates = [10000, 10050, 10100, 10200, 10300, 10500, 11000, 12000, 15000]

print(f"{'Diff %':<10} | {'Old Score':<10} | {'New Score':<10}")
print("-" * 35)

for c in candidates:
    diff = abs(target - c) / target
    old = calculate_score_old(target, c)
    new = calculate_score_new(target, c)
    print(f"{diff*100:<9.1f}% | {old:<10.1f} | {new:<10.1f}")
