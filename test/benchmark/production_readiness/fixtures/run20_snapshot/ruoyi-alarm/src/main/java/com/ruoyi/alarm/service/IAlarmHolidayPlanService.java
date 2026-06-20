package com.ruoyi.alarm.service;

import java.util.Date;
import java.util.List;
import com.ruoyi.alarm.domain.AlarmHolidayPlan;

/**
 * 节假日计划Service接口
 * 
 * @author ruoyi
 */
public interface IAlarmHolidayPlanService
{
    /**
     * 查询节假日计划列表
     * 
     * @param plan 节假日计划对象
     * @return 节假日计划集合
     */
    public List<AlarmHolidayPlan> selectList(AlarmHolidayPlan plan);

    /**
     * 查询节假日计划
     * 
     * @param planId 计划ID
     * @return 节假日计划
     */
    public AlarmHolidayPlan selectById(Long planId);

    /**
     * 新增节假日计划
     * 
     * @param plan 节假日计划对象
     * @return 结果
     */
    public int insert(AlarmHolidayPlan plan);

    /**
     * 修改节假日计划
     * 
     * @param plan 节假日计划对象
     * @return 结果
     */
    public int update(AlarmHolidayPlan plan);

    /**
     * 批量删除节假日计划
     * 
     * @param planIds 需要删除的计划ID
     * @return 结果
     */
    public int deleteByIds(Long[] planIds);

    /**
     * 批量导入节假日计划
     * 
     * @param plans 节假日计划集合
     * @return 结果
     */
    public int batchImport(List<AlarmHolidayPlan> plans);

    /**
     * 按日期范围查询节假日计划
     * 
     * @param startDate 开始日期
     * @param endDate 结束日期
     * @return 节假日计划集合
     */
    public List<AlarmHolidayPlan> selectByDateRange(Date startDate, Date endDate);

    /**
     * 判断指定日期是否为节假日
     * 
     * @param date 日期
     * @param strategyId 排班策略ID
     * @return 是否为节假日
     */
    public boolean isHoliday(Date date, Long strategyId);
}
