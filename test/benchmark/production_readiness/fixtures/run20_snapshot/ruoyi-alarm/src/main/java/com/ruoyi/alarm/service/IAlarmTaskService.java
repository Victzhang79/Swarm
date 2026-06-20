package com.ruoyi.alarm.service;

import java.util.List;
import com.ruoyi.alarm.domain.AlarmTask;
import com.ruoyi.alarm.domain.AlarmTaskChannel;
import com.ruoyi.alarm.domain.dto.AlarmTaskTestDTO;

/**
 * 预警任务 服务接口
 *
 * @author ruoyi
 */
public interface IAlarmTaskService
{
    /**
     * 查询预警任务
     *
     * @param taskId 预警任务ID
     * @return 预警任务
     */
    public AlarmTask selectAlarmTaskById(Long taskId);

    /**
     * 根据任务名称查询预警任务
     *
     * @param taskName 任务名称
     * @return 预警任务
     */
    public AlarmTask selectAlarmTaskByName(String taskName);

    /**
     * 查询预警任务列表
     *
     * @param alarmTask 预警任务
     * @return 预警任务集合
     */
    public List<AlarmTask> selectAlarmTaskList(AlarmTask alarmTask);

    /**
     * 新增预警任务
     *
     * @param alarmTask 预警任务
     * @return 结果
     */
    public int insertAlarmTask(AlarmTask alarmTask);

    /**
     * 修改预警任务
     *
     * @param alarmTask 预警任务
     * @return 结果
     */
    public int updateAlarmTask(AlarmTask alarmTask);

    /**
     * 批量删除预警任务
     *
     * @param taskIds 需要删除的预警任务ID
     * @return 结果
     */
    public int deleteAlarmTaskByIds(Long[] taskIds);

    /**
     * 删除预警任务
     *
     * @param taskId 预警任务ID
     * @return 结果
     */
    public int deleteAlarmTaskById(Long taskId);

    /**
     * 获取任务绑定的渠道配置列表
     *
     * @param taskId 任务ID
     * @return 渠道配置列表
     */
    public List<AlarmTaskChannel> selectAlarmTaskChannels(Long taskId);

    /**
     * 测试发送预警
     *
     * @param testDTO 测试参数
     * @return 结果
     */
    public boolean testSend(AlarmTaskTestDTO testDTO);
}
